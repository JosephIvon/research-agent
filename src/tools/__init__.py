"""工具模块"""
from .search_tool import SearchTool, search_tool
from .comparison_tool import ComparisonAnalyzer, infer_competitor_name
from .quality_scorer import QualityScorer, calculate_report_quality, QualityReport
from .followup_optimizer import FollowUpOptimizer, follow_up_optimizer
from .multi_role_reviewer import MultiRoleReviewer, multi_role_reviewer
from .prd_generator import PRDGenerator, prd_generator