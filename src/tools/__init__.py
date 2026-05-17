"""工具模块"""
from .search_tool import SearchTool, get_search_tool
from .comparison_tool import ComparisonAnalyzer, infer_competitor_name
from .quality_scorer import QualityScorer, calculate_report_quality, QualityReport
from .followup_optimizer import FollowUpOptimizer, get_follow_up_optimizer
from .multi_role_reviewer import MultiRoleReviewer, get_multi_role_reviewer
from .prd_generator import PRDGenerator, get_prd_generator
