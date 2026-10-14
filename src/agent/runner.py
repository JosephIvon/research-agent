"""有界 ReAct Runner：模型自主决定调用什么工具，直到给出终答或触发上界。

三重预算（步数 / token / 墙钟）+ 两层收束兜底（重复调用、空观察），
所有终止路径都有显式 stop_reason——不存在"悄悄停了"的状态。

硬约束：工具只能经 ToolRegistry.execute 执行（白名单 + 入参校验 + 超时 +
统一返回结构），Runner 不允许绕过注册表直调爬虫/搜索客户端。
"""
import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from src.agent import config
from src.agent.context import estimate_tokens, render_observation
from src.agent.tools import ToolRegistry, dedup_key, result_is_empty
from src.agent.tool_specs import build_default_tools, system_prompt
from src.agent.trace import AgentResult, AgentStep
from src.llm.token_budget import TaskBudget

logger = logging.getLogger(__name__)


class ReActRunner:
    """一次 run() = 一次有界 ReAct 任务。实例可复用但状态不跨任务残留。"""

    def __init__(
        self,
        llm_client: Any,
        tools: Optional[ToolRegistry] = None,
        workspace: Optional[Dict[str, Any]] = None,
        max_steps: Optional[int] = None,
        token_budget: Optional[int] = None,
        wall_clock_budget: Optional[float] = None,
        budget: Optional["TaskBudget"] = None,
        on_step: Optional[Callable[[AgentStep], Any]] = None,
    ):
        self.llm = llm_client
        self.workspace = workspace if workspace is not None else {}
        # TaskBudget（M6）：token+USD 双口径记账，真实 usage 优先；提供时
        # 与 token_budget 参数并存（TaskBudget 更严的一方先生效）。
        # 必须早于 build_default_tools 赋值——工具闭包要拿它给 extract_facts 记账。
        self.budget = budget
        self.tools = tools or build_default_tools(llm_client, self.workspace, budget=budget)
        self.max_steps = max_steps if max_steps is not None else config.AGENT_MAX_STEPS
        self.token_budget = token_budget if token_budget is not None else config.AGENT_TOKEN_BUDGET
        self.wall_clock_budget = (
            wall_clock_budget if wall_clock_budget is not None else config.AGENT_WALL_CLOCK_BUDGET
        )
        self.on_step = on_step

    async def run(self, user_query: str, target_urls: Optional[List[str]] = None) -> AgentResult:
        started = time.monotonic()
        schemas = self.tools.schemas()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt(schemas, self.max_steps)},
            {"role": "user", "content": self._format_task(user_query, target_urls)},
        ]

        steps: List[AgentStep] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        # usage_estimated 口径对齐 TaskBudget.snapshot()["estimated"]：只要有一轮
        # 拿不到 provider 真值 usage 就整体算估算；一轮都没跑（开跑即超额）同样算估算，
        # 免得把 0 记账当成真值报出去。预算收敛那一次也计入这里的两个计数器。
        rounds = {"real": 0, "estimated": 0}
        duplicate_counts: Dict[str, int] = {}
        nudged_duplicate = False
        empty_rounds = 0
        empty_model_rounds = 0
        stop_reason: Optional[str] = None
        answer: Optional[str] = None

        for index in range(1, self.max_steps + 1):
            # ---- 预算守卫（每轮开始先查，保证触发即停、理由明确）----
            elapsed = time.monotonic() - started
            if usage["prompt_tokens"] + usage["completion_tokens"] > self.token_budget:
                stop_reason = "budget_exceeded"
                break
            if elapsed > self.wall_clock_budget:
                stop_reason = "budget_exceeded"  # 墙钟与 token 同归"预算耗尽"，trace 里有耗时
                break
            if self.budget is not None:
                budget_reason = self.budget.exceeded_reason()
                if budget_reason is not None:
                    logger.info("[react] %s，收束为部分结果", budget_reason)
                    stop_reason = "budget_exceeded"
                    break

            step = AgentStep(index=index)
            step_start = time.monotonic()

            try:
                raw = await self.llm.achat_completion_raw(
                    messages, tools=schemas, tool_choice="auto", temperature=0.2
                )
            except Exception as exc:
                logger.warning("react runner LLM 调用失败: %s", exc)
                step.thought = f"[LLM 调用失败] {type(exc).__name__}: {exc}"
                step.elapsed = round(time.monotonic() - step_start, 4)
                stop_reason = "error"
                step.stop_reason_if_any = stop_reason
                steps.append(step)
                break

            _account_usage(usage, raw, messages)
            rounds["estimated" if _round_usage_is_estimated(raw) else "real"] += 1
            self._record_budget(raw, messages)
            content = raw.get("content")
            tool_calls = list(raw.get("tool_calls") or [])
            step.thought = (content or None) if not tool_calls else (content or None)

            # ---- 空响应兜底：既无文本也无工具调用 ----
            if not tool_calls and not (content or "").strip():
                empty_model_rounds += 1
                if empty_model_rounds >= config.MODEL_EMPTY_RESPONSE_LIMIT:
                    stop_reason = "error"
                    step.stop_reason_if_any = stop_reason
                    steps.append(step)
                    break
                messages.append({
                    "role": "user",
                    "content": "请调用工具继续调研，或直接给出最终答案。",
                })
                step.elapsed = round(time.monotonic() - step_start, 4)
                steps.append(step)
                _emit_step(self.on_step, step)
                continue
            empty_model_rounds = 0

            # ---- 终答：无工具调用且有正文 ----
            if not tool_calls:
                answer = content
                step.elapsed = round(time.monotonic() - step_start, 4)
                stop_reason = "halt"
                step.stop_reason_if_any = stop_reason
                steps.append(step)
                break

            # ---- 执行工具（全部经注册表）----
            round_has_duplicate_overflow = False
            round_all_empty = bool(tool_calls)
            assistant_msg = {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": tc.get("id") or f"call_{index}_{i}",
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc.get("arguments") or "{}"},
                    }
                    for i, tc in enumerate(tool_calls)
                ],
            }
            messages.append(assistant_msg)

            for i, tc in enumerate(tool_calls):
                name = tc.get("name") or ""
                arguments = _parse_arguments(tc.get("arguments"))
                step.tool_calls.append({"id": tc.get("id"), "name": name, "arguments": arguments})

                key = dedup_key(name, arguments)
                duplicate_counts[key] = duplicate_counts.get(key, 0) + 1
                if duplicate_counts[key] >= config.TOOL_DUPLICATE_LIMIT:
                    if nudged_duplicate:
                        round_has_duplicate_overflow = True
                        step.observations.append({
                            "ok": False, "summary": None,
                            "error": "重复调用达到上限，本条不再执行", "elapsed": 0.0,
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id") or f"call_{index}_{i}",
                            "content": json.dumps(
                                {"ok": False, "error": "重复调用达到上限，请勿再次执行相同调用"},
                                ensure_ascii=False,
                            ),
                        })
                        continue
                    messages.append({
                        "role": "user",
                        "content": "检测到你重复调用相同工具与参数。请基于已有信息继续或收敛到最终答案。",
                    })
                    nudged_duplicate = True

                result = await self.tools.execute(name, arguments)
                step.observations.append({
                    "ok": result["ok"],
                    "summary": _summarize(result),
                    "error": result["error"],
                    "elapsed": result["elapsed"],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or f"call_{index}_{i}",
                    "content": render_observation(result),
                })
                if not result_is_empty(result):
                    round_all_empty = False

            step.elapsed = round(time.monotonic() - step_start, 4)
            if round_has_duplicate_overflow:
                stop_reason = "duplicate_loop"
            empty_rounds = empty_rounds + 1 if round_all_empty else 0
            if stop_reason is None and empty_rounds >= config.EMPTY_OBSERVATION_LIMIT:
                stop_reason = "empty_observations"

            # 终止原因先落回本轮 step 再外发：on_step 回调（workflow 的 agent_step 事件）
            # 只有在这之前写入才能拿到真值，而不是恒为 None
            step.stop_reason_if_any = stop_reason
            steps.append(step)
            _emit_step(self.on_step, step)

            if stop_reason is not None:
                break
        else:
            stop_reason = stop_reason or "max_steps"

        if stop_reason is None:
            stop_reason = "max_steps"

        # ---- 预算收束：强制一次有上限的收敛调用 ----
        # 没有这一步，预算触发即 break，answer 恒为 None，workflow 整单判 failed，
        # 已跑完的工具调用与事实全部丢弃——"超额 PARTIAL" 只是对外宣称。
        if answer is None and stop_reason == "budget_exceeded" and steps:
            answer = await self._converge_on_budget(messages, usage, steps, rounds)

        # 预算/步数等在轮次开始前就触发时不会新建 step，终止原因回写到最后一轮，
        # 保证 trace 里必定有一处读得到 stop_reason（workflow 的 agent_step payload 依赖它）
        if steps and steps[-1].stop_reason_if_any is None:
            steps[-1].stop_reason_if_any = stop_reason

        return AgentResult(
            answer=answer,
            stop_reason=stop_reason,
            steps=steps,
            usage={**usage, "total": usage["prompt_tokens"] + usage["completion_tokens"]},
            usage_estimated=rounds["estimated"] > 0 or rounds["real"] == 0,
            cost_report=self.budget.snapshot() if self.budget is not None else {},
            elapsed_s=round(time.monotonic() - started, 4),
            workspace=self.workspace,
        )

    async def _converge_on_budget(
        self,
        messages: List[Dict[str, Any]],
        usage: Dict[str, int],
        steps: List[AgentStep],
        rounds: Dict[str, int],
    ) -> Optional[str]:
        """预算耗尽后的最后一次收敛：只准写报告，不准再调工具。

        三条硬约束：

        1. **只调一次，且不带 tools**。再给工具就等于用超额的预算继续跑，收束失去意义。
        2. **max_tokens 有上限，开销照记**。这次调用是明知超预算仍然要花的，它必须出现在
           cost_report 里；不记就是账做不平。
        3. **失败不外溢**。收敛失败只是拿不到终答，退回原有的 failed 语义，
           不能让一次收尾尝试把整个任务带崩。

        返回收敛出的正文；拿不到就返回 None（调用方按无终答处理）。
        `rounds` 是本轮真实/估算轮数计数器，收敛这一次也要计入——
        否则 AgentResult.usage_estimated 会漏掉它，报告出的口径就是错的。
        """
        cap = config.AGENT_BUDGET_CONVERGE_MAX_TOKENS
        if cap <= 0:
            logger.info("[react] 预算收敛已关闭（AGENT_BUDGET_CONVERGE_MAX_TOKENS=0）")
            return None

        converge_messages = list(messages) + [{
            "role": "user",
            "content": (
                "预算已用尽，现在必须停止调研并交付结论。\n"
                "请只依据上面已经拿到的信息写出一份 Markdown 报告，写清三件事："
                "已确认的结论、尚未覆盖的维度、以及数据不足的原因。\n"
                "不要再调用任何工具。无法核实的地方直接写「未获取到公开数据」，"
                "**不要编造任何数字或来源**。"
            ),
        }]

        step = AgentStep(index=len(steps) + 1)
        step_start = time.monotonic()
        try:
            raw = await self.llm.achat_completion_raw(
                converge_messages,
                tools=None,
                temperature=0.2,
                max_tokens=cap,
            )
        except Exception as exc:
            logger.warning("[react] 预算收敛调用失败，退回无终答语义: %s", exc)
            step.thought = f"[预算收敛失败] {type(exc).__name__}: {exc}"
            step.elapsed = round(time.monotonic() - step_start, 4)
            step.stop_reason_if_any = "error"
            steps.append(step)
            _emit_step(self.on_step, step)
            return None

        _account_usage(usage, raw, converge_messages)
        if _round_usage_is_estimated(raw):
            rounds["estimated"] += 1
        else:
            rounds["real"] += 1
        self._record_budget(raw, converge_messages)

        content = (raw.get("content") or "").strip()
        step.thought = content or "[预算收敛] 未产出正文"
        step.elapsed = round(time.monotonic() - step_start, 4)
        step.stop_reason_if_any = "budget_exceeded"
        steps.append(step)
        _emit_step(self.on_step, step)

        return content or None

    def _record_budget(self, raw: Dict[str, Any], messages: List[Dict[str, Any]]) -> None:
        """把每轮 LLM 调用记入 TaskBudget：真实 usage 优先，缺失则估算。"""
        if self.budget is None:
            return
        provider = getattr(self.llm, "provider_name", "unknown")
        model = getattr(self.llm, "default_model", None)
        raw_usage = raw.get("usage") or {}
        prompt_tokens = raw_usage.get("prompt_tokens")
        completion_tokens = raw_usage.get("completion_tokens")
        if prompt_tokens is None or completion_tokens is None:
            self.budget.record_estimated(
                provider, model,
                prompt_text=json.dumps(messages, ensure_ascii=False, default=str),
                completion_text=str(raw.get("content") or ""),
            )
        else:
            self.budget.record(provider, model, int(prompt_tokens), int(completion_tokens))

    @staticmethod
    def _format_task(user_query: str, target_urls: Optional[List[str]]) -> str:
        lines = [f"调研任务：{user_query}"]
        if target_urls:
            lines.append(f"指定参考站点：{', '.join(target_urls)}（优先抓取这些站点）")
        lines.append("请开始。")
        return "\n".join(lines)


def _round_usage_is_estimated(raw: Dict[str, Any]) -> bool:
    """本轮是否只拿到估算值：provider 缺 prompt/completion 任一真值即为估算。"""
    raw_usage = raw.get("usage") or {}
    return raw_usage.get("prompt_tokens") is None or raw_usage.get("completion_tokens") is None


def _account_usage(usage: Dict[str, int], raw: Dict[str, Any], messages: List[Dict[str, Any]]) -> None:
    """优先采用 provider 返回的真实 usage；缺失时按字符估算（宁高勿低）。"""
    raw_usage = raw.get("usage") or {}
    prompt_tokens = raw_usage.get("prompt_tokens")
    completion_tokens = raw_usage.get("completion_tokens")
    if prompt_tokens is None:
        prompt_tokens = estimate_tokens(json.dumps(messages, ensure_ascii=False, default=str))
    if completion_tokens is None:
        completion_tokens = estimate_tokens(str(raw.get("content") or ""))
    usage["prompt_tokens"] += int(prompt_tokens)
    usage["completion_tokens"] += int(completion_tokens)


def _parse_arguments(raw_arguments: Any) -> Dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str) and raw_arguments.strip():
        try:
            parsed = json.loads(raw_arguments)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _summarize(result: Dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, list):
        return f"list[{len(data)}]"
    if isinstance(data, dict):
        keys = list(data)[:6]
        return "{" + ", ".join(keys) + "}"
    return (str(data)[:80] if data is not None else "None")


def _emit_step(on_step: Optional[Callable[[AgentStep], Any]], step: AgentStep) -> None:
    if on_step is None:
        return
    try:
        maybe = on_step(step)
        if hasattr(maybe, "__await__"):
            asyncio.create_task(maybe)
    except Exception:
        logger.debug("on_step 回调失败（不影响主流程）", exc_info=True)
