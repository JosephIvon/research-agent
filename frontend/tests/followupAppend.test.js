import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const view = readFileSync(new URL('../src/views/FollowupView.vue', import.meta.url), 'utf8')
const api = readFileSync(new URL('../src/api/index.js', import.meta.url), 'utf8')

// 「添加到报告」曾经只弹一个 toast，报告一个字没变。这里从源码层面钉住接线：
// 断言必须精确到**调用点**，只断言"文件里出现过某个词"会被把函数体换成假实现蒙混过去。

test('追问页真的调用了写回接口', () => {
  assert.match(
    view,
    /await researchApi\.appendFollowupToReport\(/,
    '「添加到报告」必须真的调后端写回，只弹 toast 等于什么都没做'
  )
})

test('写回接口指向报告的 followup 端点', () => {
  assert.match(
    api,
    /appendFollowupToReport:\s*\(reportId, payload\)\s*=>\s*api\.post\(/,
    'api 层必须提供写回方法'
  )
  assert.match(
    api,
    /\/research\/reports\/\$\{encodeURIComponent\(reportId\)\}\/followup/,
    '写回必须打到带 report id 的端点'
  )
})

test('追加成功后要刷新本地缓存的正文', () => {
  assert.match(
    view,
    /store\.updateReport\(reportId, \{ markdown: data\.markdown \}\)/,
    '本地缓存不更新的话，回到报告页看到的仍是追加前的正文'
  )
})

test('只有最近一次成功回答才允许写回', () => {
  assert.match(view, /lastAnswer\.value = \{ question, answer \}/, '成功回答必须被记下来')
  assert.match(view, /v-if="lastAnswer"/, '按钮应由"有没有成功回答"决定是否出现')
})

test('路由参数按报告 id 使用', () => {
  assert.match(view, /const reportId = route\.params\.id/, '路由参数就是报告 id')
  assert.doesNotMatch(view, /store\.getReport\(taskId\)/, '不能拿任务 id 去读报告')
})
