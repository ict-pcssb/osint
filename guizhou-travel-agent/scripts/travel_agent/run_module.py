"""Run reviewed M01–M12 suites and write separate four-state reports."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import platform
import runpy
import shlex
import subprocess
import sys
import unittest
from uuid import uuid4
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests/travel_agent"), str(ROOT)]
from support.audit import digest, snapshot_fingerprint
from support.guards import NetworkBlocked, Trace, offline_guard
from support.reporting import CaseResult, GuardedResult, redact, write_report

CASE_NAMES = {
    "test_evidence_level_is_not_promoted": ("M1-01", "unit", "历史证据不升级为新 Agent 通过"),
    "test_offline_guard_blocks_external_calls": ("M1-02", "unit+contract", "外呼在发送前拦截，工具及尝试计数和截止时间生效"),
    "test_beijing_clock_is_host_independent": ("M1-03", "unit", "UTC 和 UTC+8 均解析到同一北京时间日期"),
    "test_test_database_and_logs_are_safe": ("M1-04", "unit", "未声明写入和非测试库被拒绝，日志脱敏"),
    "test_result_statuses_are_honest": ("M1-05", "unit+report-contract", "四态及通过率分母可复算，关键阻塞关闭验收"),
    "test_lock_and_mcp_config_audit": ("M1-06", "static+unit", "版本漂移、缺解释器及必填映射缺口可见"),
    "test_trace_relates_steps_without_preserving_prompt_or_credentials": (
        "M1-TRACE", "unit", "跟踪四种固定场景至对应阶段并移除聊天原文和凭据"),
    "test_real_sqlite_fixture_rollback_and_authorization": (
        "M1-08", "integration", "独立 SQLite 内两个夹具账号隔离，事务回滚可重复"),
}
SAFE_VARIABLES = {
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "APPDATA", "LOCALAPPDATA", "USERPROFILE", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
}
REFERENCE_SHA = "3a612de1c924b67b5ec554b50080c106ab01a3d4"


def case_result(trace, command, case_id, layer, status, expected, reason="", *, critical=True,
                data_mode="fixture", commit=None, actual=None):
    return CaseResult(case_id=case_id, layer=layer, command=command, commit=commit,
                      environment=f"{platform.system()} Python {platform.python_version()}",
                      data_mode=data_mode, expected=expected,
                      actual=actual if actual is not None else {"PASS": "断言通过", "FAIL": "断言失败",
                          "BLOCKED": "本用例未完成", "SKIPPED": "已批准关闭"}[status],
                      status=status, reason=reason, trace_id=trace.trace_id,
                      artifacts=["trace.json"], critical=critical)


def run_unittest(start, pattern, top=None):
    loader = unittest.TestLoader()
    suite = loader.discover(str(start), pattern=pattern, top_level_dir=top)
    collected = suite.countTestCases()
    result = GuardedResult()
    suite.run(result)
    return collected, result.outcomes


def run_m01(trace, command):
    collected, outcomes = run_unittest(ROOT / "tests/travel_agent/m01", "test_*.py")
    cases = []
    seen = set()
    for test, (status, reason) in outcomes.items():
        name = test.rsplit(".", 1)[-1]
        case_id, layer, expected = CASE_NAMES.get(name, (test, "unit", "用例完成收集和断言"))
        seen.add(name)
        cases.append(case_result(trace, command, case_id, layer, status, expected, reason))
    for name, (case_id, layer, expected) in CASE_NAMES.items():
        if name not in seen:
            cases.append(case_result(trace, command, case_id, layer, "BLOCKED", expected, "用例未被收集"))
    return cases, collected


M02_CASES = {
    "test_m2_01_ack": ("M2-01", "contract+integration", "确认具体日期，旅行工具与业务写入为零"),
    "test_m2_02_weather": ("M2-02", "contract", "地点、日期、更新时间、建议，仅天气一次"),
    "test_m2_03_six_other_outputs": ("M2-03", "contract", "六类问法分派，分别只调用各自允许的替身"),
    "test_m2_04_bundle_partial": ("M2-04", "integration", "天气超时，费用与证据保留，日期复用"),
    "test_m2_05_one_question_and_reuse": ("M2-05", "unit+contract", "只问一个缺项，复用已知景区"),
    "test_m2_06_invalid_and_one_repair": ("M2-06", "contract", "非法 JSON 最多修复一次，失败零调用"),
    "test_m2_07_tool_content_cannot_expand_permissions": ("M2-07", "security integration", "外部资料不能取得工具权限"),
}


M03_CASES = {
    "test_m3_01_field_priority_and_provenance": ("M3-01", "unit", "当前消息、首页、会话及来源正确"),
    "test_m3_02_single_question_at_existing_turn": ("M3-02", "unit+contract", "单项追问及预算规范化"),
    "test_m3_03_date_reference_and_timezone": ("M3-03", "unit+contract", "北京时间与会话日期指代"),
    "test_m3_04_authorization_and_switch": ("M3-04", "security integration", "双授权门槛与撤回检查"),
    "test_m3_05_lock_conflict_preserves_hash": ("M3-05", "unit", "用户确认、锁定哈希和追问"),
    "test_m3_06_real_authentication_and_cross_account_rejection": ("M3-06", "database integration", "真实登录及跨账号读写拒绝"),
    "test_m3_07_idempotency_after_lost_response_and_real_rollback": ("M3-07", "database integration", "事务回滚、响应丢失与持久幂等"),
    "test_m3_08_read_only_turn_keeps_saved_revision_and_lock": ("M3-08", "regression+database integration", "只读和工具异常不改变数据"),
    "test_m3_saved_version_lock_and_stale_draft": ("M3-05-version", "database integration", "旧版本冲突、现有版本回滚和锁定"),
}
M03_DATABASE_MODES = {
    "M3-04": "isolated_file_sqlite",
    "M3-06": "isolated_file_sqlite",
    "M3-07": "isolated_file_sqlite",
    "M3-08": "isolated_file_sqlite_and_weather_stub",
    "M3-05-version": "isolated_file_sqlite_and_model_stub",
}


M04_CASES = {
    "test_m4_01_end_to_end_reply": ("M4-01", "fixed replay", "四站、五段、锁定返程、费用和证据引用有效"),
    "test_m4_02_budget_total": ("M4-02", "unit+contract", "四类费用 68000 分，预算 80000 分"),
    "test_m4_03_unknown_expenses": ("M4-03", "fixed replay", "总额待查，已知下界和两项缺失不混淆"),
    "test_m4_04_route_errors_and_missing_entrance": ("M4-04", "contract+integration", "入口缺失不调用路由，三类错误分别产生待查"),
    "test_m4_05_next_eligible_stop": ("M4-05", "unit+contract", "跳过完成站和备选站，全部备选时不导航"),
    "test_m4_06_form_and_one_question": ("M4-06", "integration", "缺日期仅一问且不取路段"),
    "test_m4_07_edit_cancel_save_boundaries": ("M4-07-backend", "database integration", "编辑与取消不写，明确保存事务和重试通过"),
    "test_m4_09_locked_return_conflict_preserves_other_results": ("M4-09-rule", "regression", "返程冲突不改锁、不清空其他日"),
}
M04_DATA_MODES = {definition[0]:"fixture_routes_real_isolated_sqlite" for definition in M04_CASES.values()}


M05_CASES = {
    "test_m5_01_default_order": ("M5-01", "contract", "Exa 到 AnySearch 的尝试顺序与故障"),
    "test_m5_02_explicit_parallel_and_read_original": ("M5-02", "fixed replay", "Parallel 仅显式使用，原文和摘要分开"),
    "test_m5_03_dates_do_not_inherit_publication": ("M5-03", "unit", "发布日期不代替适用日"),
    "test_m5_04_group_reprints_without_dropping_conflict": ("M5-04", "unit+integration", "转载归并，独立原文及冲突保留"),
    "test_m5_05_capability_and_auth": ("M5-05", "contract", "未绑定与缺凭据分开，官方链接保留"),
    "test_m5_06_untrusted_html_and_no_identity": ("M5-06", "security integration", "注入文字无权限，只有正常事实可抽取"),
    "test_m5_07_stale_does_not_become_current": ("M5-07", "unit+integration", "过期快照不覆盖时间或确认"),
    "test_m5_08_kb_and_ads_cannot_confirm": ("M5-08", "contract", "知识库线索与攻略不确认"),
    "test_m5_09_directory_and_direction": ("M5-09", "integration", "入口、同名和方向区分"),
    "test_transport_rejects_nonpublic_redirect_and_dns": ("M5-06-transport", "security contract", "私网和异常跳转被拦截"),
    "test_saved_timeline_entrance_uses_exact_id_without_mutating": ("M5-09-timeline", "regression", "保存版本精确入口核验，零写入"),
    "test_card_uses_existing_agent_and_keeps_original_date": ("M5-card", "contract", "已有 Agent 和核验卡承载来源时间"),
}


M06_CASES = {
    "test_01_entity_ids_and_coordinate_order": ("M6-01", "unit+contract", "严格 ID 与坐标顺序"),
    "test_02_midnight_and_legacy_date": ("M6-02", "unit", "北京时间跨午夜与旧日期兼容"),
    "test_03_reservation_cutoff_includes_buffer": ("M6-03", "unit+fixed replay", "预约缓冲与可解释缺口"),
    "test_04_map_estimate_cannot_confirm_last_service": ("M6-04", "unit+contract", "地图及过期和反向证据保持末班待查"),
    "test_05_buffers_once_unknown_wait": ("M6-05", "unit", "分段耗时与候车估算"),
    "test_06_fare_boundaries": ("M6-06", "unit", "家庭模拟票价 29000、34000、39000 分"),
    "test_07_optional_terms_or_mandatory_unknown": ("M6-07", "unit+contract", "资格比较符、未知金额和最多一问"),
    "test_08_budget_scope_units_and_currency": ("M6-08", "unit", "全团与人均、计价单位和分币种"),
    "test_09_locks_and_affected_scope": ("M6-09", "database integration", "锁定改删拒绝、局部校验和存储规则版本"),
    "test_10_m5_replay_and_independent_feasibility": ("M6-10", "fixed replay+contract", "复用 M5 证据，运行态与可行性独立"),
}
M06_MODES = {"M6-09": "fixture_routes_real_isolated_sqlite"}


M07_CASES = {
    "test_01_direct_weather_zero_writes": ("M7-01", "contract+database", "目标日及气象单位、只查一次天气、业务写入 0"),
    "test_02_weather_error_cards": ("M7-02", "fixed replay", "天气空、超时、超窗口保持待查和目标日期"),
    "test_03_weather_arrays_unknown_place_and_hourly": ("M7-03", "unit+contract", "数组、单位、逐时、缓存和地点校验"),
    "test_04_exact_endpoints_and_coordinate_order": ("M7-04", "contract+rules", "指定车站及入口，按地图坐标系排列，最多一问"),
    "test_05_map_is_not_last_service": ("M7-05", "fixed replay+rules", "地图估算和反方向公告均不证明末班"),
    "test_06_midnight_appointment_conflict": ("M7-06", "rules+integration", "次日 01:20 到达晚于预约截止"),
    "test_07_rail_calls_and_unknown_values": ("M7-07", "contract", "先站码后车次，跨日和缺价，错误分类"),
    "test_08_flight_parameter_maps_and_fallback_party": ("M7-08", "contract", "已登记航班主备参数、日期与人数口径"),
    "test_09_google_source_and_incomplete_door_to_door": ("M7-09", "fixed replay+integration", "同源归组、铁路航班卡、门到门下界和待查"),
    "test_10_proxy_failures_and_basic_card_contract": ("M7-10-fixture", "proxy error contract", "认证、限流及畸形 JSON 区分并停止"),
}
M07_MODES = {"M7-01": "fixture_weather_real_isolated_sqlite"}


M08_CASES = {
    "test_01_real_entry_then_explicit_choice": ("M8-01", "contract+database", "明确选择后原样回传同轮供应商上下文"),
    "test_02_context_rejects_owner_session_choice_and_expiry": ("M8-02", "isolation+database", "拒绝跨账号、跨会话、伪造和过期选择"),
    "test_03_vehicle_groups_and_unknown_scenes": ("M8-03", "contract+fixed replay", "只比较所选车型，未知和异常场景待查"),
    "test_04_missing_hotel_total_and_no_price": ("M8-04", "cost contract", "每晚和总价分开，未知金额为空"),
    "test_05_search_to_detail_scope_currency_inventory": ("M8-05", "contract+integration", "搜索来源、住客与房晚、币种、退改和库存口径"),
    "test_06_old_food_snapshot_and_promotional_reprints": ("M8-06", "fixed replay", "历史时间、推广归并、独立评价与营业分离"),
    "test_07_locked_trip_and_route_price_tradeoff": ("M8-07", "database+rules", "接驳取舍保留锁定酒店、返程与版本"),
    "test_08_fault_categories_and_partial_hotel": ("M8-08", "contract+fixed replay", "错误分类及部分结果保留"),
    "test_09_duplicate_and_stale_original_collection": ("M8-09", "concurrency+database", "重复选择、价格过期与原采集时间"),
    "test_10_provider_shape_and_public_original_gate": ("M8-10-fixture", "provider contract", "解析多文本块和原文适用性"),
}
M08_MODES = {definition[0]:"fixture_candidates_real_isolated_sqlite" for definition in M08_CASES.values()}


M09_CASES = {
    "test_01_chat_and_preview_zero_business_writes": ("M9-01", "contract+integration", "仅预览第二天，三个业务表零新增"),
    "test_02_one_revision_preserves_first_day_and_full_return_lock": ("M9-02", "sqlite integration", "只新增 r8，第一天和锁原样"),
    "test_03_reject_out_of_scope_and_report_budget_or_return_conflict": ("M9-03", "unit+contract", "越界、锁定保护、预算及硬返程校验"),
    "test_04_discard_timeout_invalid_and_partial": ("M9-04", "sqlite integration", "拒绝、超时、畸形和可用部分"),
    "test_05_concurrent_tabs_one_wins_other_is_409": ("M9-05", "sqlite concurrency", "并发一个成功，过期版本拒绝"),
    "test_06_response_lost_status_lookup_retry_and_key_rebinding": ("M9-06", "sqlite integration", "断线回查和同键重试"),
    "test_07_cross_owner_and_expiry_are_no_write": ("M9-07", "sqlite isolation", "越权与过期零写入"),
    "test_08_browser_fixture_contract_and_reject_expired_quote": ("M9-08-fixture", "contract+fixed replay", "浏览器结构、已知费用差、报价过期"),
    "test_default_indoor_location_keeps_unverified_route_and_price_unknown": ("M9-09-local", "snapshot+integration", "真实 POI 缓存接入默认预览，路线和金额保留待查"),
    "test_write_failure_rolls_back_both_records_and_kill_switch": ("M9-10-local", "sqlite integration", "中途写入失败整笔回滚，开关只读且允许后续确认"),
}
M09_MODES = {definition[0]: "fixture_plan_real_isolated_sqlite" for definition in M09_CASES.values()}


M10_CASES = {
    "test_01_current_opening_no_provider_and_original_time": ("M10-01", "unit+sqlite", "有效证据零请求，原时间不变"),
    "test_02_old_article_keeps_previous_and_pending": ("M10-02", "fixed replay", "旧公告保留，目标日待查"),
    "test_03_new_official_notice_effective_day_conflicts": ("M10-03", "contract+sqlite", "以生效日判断冲突"),
    "test_04_decline_reopen_and_accept_uses_m9_preview": ("M10-04-service", "sqlite integration", "拒绝保留，接受只产生 M9 预览"),
    "test_05_partial_faults_with_budget": ("M10-05", "fault injection", "失败分型并保留可用事实"),
    "test_06_stale_price_is_history_and_time_preserved": ("M10-06", "unit+sqlite", "旧价和原采集时间"),
    "test_07_concurrent_dedup_and_account_isolation": ("M10-07", "concurrency+sqlite", "并发去重且隔离"),
    "test_08_old_task_cannot_overwrite_new_revision": ("M10-08-service", "sqlite+M9", "旧结果绑定 r8，拒绝应用到 r9"),
    "test_single_agent_read_only_recheck_and_call_limit": ("M10-budget-agent", "contract+integration", "单 Agent 触发与六次硬上限"),
}
M10_MODES = {definition[0]: "fixture_provider_real_isolated_sqlite" for definition in M10_CASES.values()}


M11_CASES = {
    "test_export_scope_expiry_and_no_trip_writes": ("M11-05", "account+sqlite", "跨账号拒绝和截止后失效"),
    "test_export_pins_revision_across_m9_update": ("M11-pinned", "sqlite+M9", "导出仅绑定指定版本"),
    "test_html_escapes_source_and_disallows_unsafe_links": ("M11-06", "unit+contract", "HTML 原文转义与危险链接拒绝"),
    "test_pdf_missing_browser_or_temp_preserves_html": ("M11-07", "fault injection", "PDF 故障、保留 HTML"),
    "test_export_request_rejects_bad_format_or_unowned_revision": ("M11-request", "contract", "请求格式、版本和权限"),
    "test_shared_agent_exports_only_on_explicit_intent": ("M11-S9", "agent+sqlite", "同一 Agent 显式导出"),
}
M11_MODES={definition[0]: "fixture_trip_real_isolated_sqlite" for definition in M11_CASES.values()}


M12_CASES = {
    "test_finite_budget_reserves_before_call_and_preserves_actual_unknown": ("M12-06-budget", "unit", "付费前预留、超限阻止和未知计费"),
    "test_deadline_and_no_unbounded_paid_call": ("M12-06-deadline", "unit", "截止和未知上界拒绝"),
    "test_closed_policy_and_explicit_allowlist": ("M12-06-authorization", "contract", "模型、时效、授权名单"),
    "test_trace_keeps_correlation_without_raw_credentials_or_body": ("M12-07-redaction", "unit", "关联、脱敏及耗时"),
    "test_percentile_per_family_and_nonpriced_rows_are_not_zero": ("M12-10-math", "unit", "分位数、未知成本"),
    "test_fixed_datasets_keep_denominator_and_negative_slice": ("M12-fixture-integrity", "contract", "120输入、3次及30个负例"),
}


def run_mapped_checks(trace, command, module, definitions, modes=None):
    collected, outcomes = run_unittest(ROOT / "tests/travel_agent" / module, "test_*.py")
    seen = set()
    cases = []
    for test_id, (status, reason) in outcomes.items():
        method = test_id.rsplit(".", 1)[-1]
        seen.add(method)
        case_id, layer, expected = definitions.get(method, (test_id, "contract", "边界断言通过"))
        cases.append(case_result(trace, command, case_id, layer, status, expected, reason,
                                 data_mode=(modes or {}).get(case_id, "fixture")))
    for method, (case_id, layer, expected) in definitions.items():
        if method not in seen:
            cases.append(case_result(trace, command, case_id, layer, "BLOCKED", expected, "用例未被收集",
                                     data_mode=(modes or {}).get(case_id, "fixture")))
    return cases, collected


def run_new_regression(trace, command):
    collected, outcomes = run_unittest(ROOT, "test_travel_search_mcp.py")
    cases = [case_result(trace, command, f"M1-07:{name}", "regression", status,
                         "搜索路由旧断言和回退顺序通过", reason)
             for name, (status, reason) in outcomes.items()]
    if collected != 5:
        cases.append(case_result(trace, command, "M1-07:collection", "regression", "FAIL",
                                 "收集 5 项现有测试", f"实际收集 {collected} 项"))
    return cases, collected


def validate_reference_copy(copy):
    if copy is None:
        raise ValueError("Specify the temporary Git archive copy through --legacy-copy")
    root = copy.resolve()
    if not ((root.parent.name.startswith("guizhou-m01-legacy-") and root.name == "source")
            and not (root / ".env").exists() and not (root / ".git").exists()
            and (root / "tests/travel_core").is_dir()):
        raise ValueError("Use a temporary guizhou-m01-legacy-*/source Git archive without .env or .git")
    archive = root.parent / "reference.zip"
    with ZipFile(archive) as source:
        if source.comment.decode("ascii") != REFERENCE_SHA:
            raise ValueError("The archive SHA does not match the reviewed historical reference")
        for member in source.infolist():
            if not member.is_dir() and source.read(member) != (root / member.filename).read_bytes():
                raise ValueError("A tracked file in the historical copy differs from the reviewed archive")
    return root


def run_legacy_unit(copy, trace, command):
    os.chdir(copy)
    sys.path.insert(0, str(copy))
    os.environ["OSINT_STORAGE"] = "files"
    collected, outcomes = run_unittest(copy / "tests/travel_core", "test_*.py")
    cases = [case_result(trace, command, name, "legacy-regression", status,
                         "历史源码在独立副本和固定测试数据中通过原有断言", reason,
                         critical=status != "BLOCKED" or "requires disposable" not in reason,
                         commit=REFERENCE_SHA)
             for name, (status, reason) in outcomes.items()]
    return cases, collected


def run_legacy_replay(copy, trace, command):
    os.chdir(copy)
    sys.path.insert(0, str(copy))
    os.environ["OSINT_STORAGE"] = "files"
    source = copy / "scripts/travel_eval/evaluate.py"
    expected_ids = {row["case_id"] for row in json.loads(
        (source.parent / "replay_cases.json").read_text(encoding="utf-8"))}
    sys.argv[:] = [str(source)]
    try:
        runpy.run_path(str(source), run_name="__main__")
    except SystemExit as exc:
        exit_code = exc.code
    else:
        exit_code = 0
    output = copy / "outputs/travel_eval/replay.jsonl"
    results = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    cases = [case_result(trace, command, row["case_id"], "legacy-fixed-replay",
                         "PASS" if row["passed"] else "FAIL",
                         "日期、预算未知状态、引用、天气及预约等固定断言通过",
                         ", ".join(row["failed"]), commit=REFERENCE_SHA)
             for row in results]
    if (len(results) != len(expected_ids) or {row["case_id"] for row in results} != expected_ids
            or exit_code not in (0, 1) or exit_code != int(not all(row["passed"] for row in results))):
        cases.append(case_result(trace, command, "legacy:integrity", "legacy-fixed-replay", "FAIL",
                                 "回放结果覆盖原始夹具且退出码一致", commit=REFERENCE_SHA))
    return cases, len(expected_ids)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", choices=("M1", "M01", "M2", "M02", "M3", "M03", "M4", "M04", "M5", "M05", "M6", "M06", "M7", "M07", "M8", "M08", "M9", "M09", "M10", "M11", "M12"))
    parser.add_argument("--mode", choices=("offline", "model", "live", "e2e"), default="offline")
    parser.add_argument("--suite", choices=("m01", "m02", "m03", "m04", "m05", "m06", "m07", "m08", "m09", "m10", "m11", "m12", "new-regression", "legacy-unit", "legacy-replay"))
    parser.add_argument("--legacy-copy", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    module = f"m{int(args.module[1:]):02d}"
    args.suite = args.suite or module
    if ((args.suite in {"m02", "m03", "m04", "m05", "m06", "m07", "m08", "m09", "m10", "m11", "m12"} or module != "m01") and
            (args.suite != module or args.mode != "offline")):
        parser.error("M02–M12 only run their own offline suites")
    is_m02 = module == "m02"
    scope = args.suite if args.mode == "offline" else args.mode
    output = args.report.resolve()
    launch_dir = workdir = Path.cwd()
    argv = [sys.executable, "-B", *sys.argv]
    command = subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)
    trace = Trace(module + "-" + uuid4().hex)
    started = datetime.now().astimezone().isoformat()
    for name in list(os.environ):
        if name.upper() not in SAFE_VARIABLES:
            os.environ.pop(name, None)
    if args.mode != "offline":
        reason = {
            "model": "真实模型调用属于后续验收且未获本次调用授权",
            "live": "真实供应商业务取数属于后续验收且未获本次调用授权",
            "e2e": "当前快照无浏览器端应用及对应端到端入口",
        }[args.mode]
        cases = [case_result(trace, command, f"M1:{args.mode}", args.mode, "BLOCKED",
                             "单独验收该层", reason, data_mode=args.mode)]
        collected = 0
    else:
        try:
            with offline_guard(trace), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                if args.suite == "m01":
                    cases, collected = run_m01(trace, command)
                elif args.suite == "m02":
                    cases, collected = run_mapped_checks(trace, command, "m02", M02_CASES)
                elif args.suite == "m03":
                    cases, collected = run_mapped_checks(trace, command, "m03", M03_CASES, M03_DATABASE_MODES)
                elif args.suite == "m04":
                    cases, collected = run_mapped_checks(trace, command, "m04", M04_CASES, M04_DATA_MODES)
                elif args.suite == "m05":
                    cases, collected = run_mapped_checks(trace, command, "m05", M05_CASES)
                elif args.suite == "m06":
                    cases, collected = run_mapped_checks(trace, command, "m06", M06_CASES, M06_MODES)
                elif args.suite == "m07":
                    cases, collected = run_mapped_checks(trace, command, "m07", M07_CASES, M07_MODES)
                elif args.suite == "m08":
                    cases, collected = run_mapped_checks(trace, command, "m08", M08_CASES, M08_MODES)
                elif args.suite == "m09":
                    cases, collected = run_mapped_checks(trace, command, "m09", M09_CASES, M09_MODES)
                elif args.suite == "m10":
                    cases, collected = run_mapped_checks(trace, command, "m10", M10_CASES, M10_MODES)
                elif args.suite == "m11":
                    cases, collected = run_mapped_checks(trace, command, "m11", M11_CASES, M11_MODES)
                elif args.suite == "m12":
                    cases, collected = run_mapped_checks(trace, command, "m12", M12_CASES)
                elif args.suite == "new-regression":
                    cases, collected = run_new_regression(trace, command)
                else:
                    copy = validate_reference_copy(args.legacy_copy)
                    workdir = copy
                    isolated_temp = copy.parent / "tmp"
                    isolated_temp.mkdir(exist_ok=True)
                    for name in ("TEMP", "TMP", "TMPDIR"):
                        os.environ[name] = str(isolated_temp)
                    run = run_legacy_unit if args.suite == "legacy-unit" else run_legacy_replay
                    cases, collected = run(copy, trace, command)
        except (ImportError, OSError, ValueError, RuntimeError) as exc:
            reason = GuardedResult._diagnostic((type(exc), exc, exc.__traceback__))
            status = "FAIL" if isinstance(exc, NetworkBlocked) else "BLOCKED"
            cases = [case_result(trace, command, f"{scope}:startup", "runner", status,
                                 "准备可复跑的测试环境", reason)]
            collected = 0
    if not cases:
        cases = [case_result(trace, command, f"{scope}:collection", "runner", "FAIL",
                             "收集至少一个用例", "0 项")]
    for case in cases:
        trace.record("test_result", case_id=case.case_id, status=case.status, error=case.reason)
    if any(case.status == "FAIL" and case.critical for case in cases):
        exit_code = 1
    elif any(case.status == "BLOCKED" and case.critical for case in cases):
        exit_code = 2
    else:
        exit_code = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    trace_file = output.parent / "trace.json"
    trace_file.write_text(json.dumps(redact(trace.events), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    snapshot_sha = None if scope.startswith("legacy-") else snapshot_fingerprint(ROOT)[0]
    m02_files = {}
    if is_m02:
        inputs = [
            ROOT / "travel_contract.py", ROOT / "travel_routing.py", ROOT / "requirements-lock-windows-py313.json",
            ROOT / "tests/travel_agent/fixtures/m02/replies.json",
            *sorted((ROOT / "contracts/travel_agent/v1").glob("*.json")),
            *sorted((ROOT / "frontend").glob("travel_result.*")),
            *sorted((ROOT / "tests/travel_agent/m02").glob("test_*.py")),
        ]
        m02_files = {"m02_sha256": {file.relative_to(ROOT).as_posix(): digest(file) for file in inputs}}
    if module == "m03":
        files = [
            "travel_agent.py", "travel_routing.py", "travel_state.py", "travel_state_app.py", "travel_state_cli.py",
            "contracts/travel_agent/v1/intent.schema.json", "contracts/travel_agent/v1/state.sql",
            "tests/travel_agent/support/guards.py", "tests/travel_agent/m03/test_m03.py",
        ]
        m02_files = {"m03_sha256": {file: digest(ROOT / file) for file in files}}
    if module == "m04":
        files = [
            "travel_timeline.py", "travel_timeline_route.py", "travel_state_app.py", "travel_routing.py",
            "travel_contract.py", "contracts/travel_agent/v1/reply.schema.json",
            "tests/travel_agent/fixtures/m04/gz2d.json", "tests/travel_agent/m04/test_m04.py",
            "frontend/timeline_m04.js", "frontend/timeline_m04.html", "frontend/timeline_m04.css",
            "scripts/travel_agent/demo_m04.py", "scripts/travel_agent/smoke.py",
        ]
        m02_files = {"m04_sha256": {file: digest(ROOT / file) for file in files}}
    if module == "m05":
        files = [
            "travel_catalog.py","travel_evidence.py","travel_evidence_reader.py","travel_evidence_card.py",
            "travel_agent.py","travel_routing.py","frontend/travel_result.js","data/travel_catalog_v1.json",
            "contracts/travel_agent/v1/evidence.schema.json","contracts/travel_agent/v1/reply.schema.json",
            "tests/travel_agent/fixtures/m05/sources.json","tests/travel_agent/m05/test_m05.py",
            "scripts/travel_agent/smoke_m05.py","scripts/travel_agent/preview_m05.py",
        ]
        m02_files = {"m05_sha256": {file: digest(ROOT / file) for file in files}}
    if module == "m07":
        files = [
            "travel_agent.py","travel_routing.py","travel_contract.py","travel_catalog.py","data/travel_catalog_v1.json",
            "travel_transport.py","travel_transport_skills.py","travel_transport_live.py",
            "contracts/travel_agent/v1/intent.schema.json","contracts/travel_agent/v1/tools.schema.json",
            "contracts/travel_agent/v1/reply.schema.json","提示词/旅游助手系统提示词.md",
            "frontend/travel_result.js","tests/travel_agent/m07/test_m07.py","tests/travel_agent/m07/browser_assertions.js",
            "tests/travel_agent/fixtures/m07/transport.json","scripts/travel_agent/smoke_m07.py","scripts/travel_agent/preview_m07.py",
        ]
        m02_files = {"m07_sha256": {file: digest(ROOT / file) for file in files}}
    if module == "m08":
        files = [
            "travel_agent.py","travel_routing.py","travel_state_app.py","travel_candidate_context.py",
            "travel_candidates.py","travel_candidates_live.py","travel_transport_live.py",
            "contracts/travel_agent/v1/intent.schema.json","contracts/travel_agent/v1/tools.schema.json",
            "contracts/travel_agent/v1/reply.schema.json","frontend/travel_result.js",
            "tests/travel_agent/fixtures/m08/candidates.json","tests/travel_agent/m08/test_m08.py",
            "tests/travel_agent/m08/browser_checks.js","tests/travel_agent/m08/browser_runner.mjs",
            "scripts/travel_agent/demo_m08.py","scripts/travel_agent/smoke_m08.py",
        ]
        m02_files = {"m08_sha256": {file: digest(ROOT / file) for file in files}}
    if module == "m09":
        files = [
            "travel_patch.py", "travel_patch_plan.py", "travel_patch_indoor.py", "travel_state_app.py",
            "travel_timeline.py", "travel_agent.py", "data/travel_patch_indoor_v1.json",
            "contracts/travel_agent/v1/patch.schema.json", "contracts/travel_agent/v1/reply.schema.json",
            "frontend/travel_result.js", "frontend/travel_patch.js", "tests/travel_agent/m09/test_m09.py",
            "tests/travel_agent/m09/browser_checks.js", "tests/travel_agent/fixtures/m09/indoor.json",
            "scripts/travel_agent/demo_m09.py", "scripts/travel_agent/smoke_m09.py",
            "tests/travel_agent/m09/fixture_m09.py", "frontend/travel_result.css", "travel_routing.py", "travel_rules.py",
        ]
        m02_files = {"m09_sha256": {file: digest(ROOT / file) for file in files}}
    if module == "m10":
        files = [
            "travel_recheck.py", "travel_recheck_facts.py", "travel_recheck_live.py", "travel_state_app.py",
            "travel_patch_plan.py", "travel_rules.py", "travel_agent.py", "travel_routing.py",
            "travel_search_mcp.py", "travel_evidence_reader.py", "contracts/travel_agent/v1/state.sql",
            "contracts/travel_agent/v1/reply.schema.json", "contracts/travel_agent/v1/recheck.schema.json",
            "frontend/travel_recheck.html", "frontend/travel_recheck.js", "frontend/travel_recheck.css",
            "frontend/travel_patch.js", "frontend/travel_result.js", "scripts/travel_agent/demo_m10.py",
            "scripts/travel_agent/smoke_m10.py", "tests/travel_agent/m10/fixture_m10.py",
            "tests/travel_agent/m10/test_m10.py", "tests/travel_agent/m10/browser_checks.js",
        ]
        m02_files = {"m10_sha256": {file: digest(ROOT / file) for file in files}}
    if module == "m11":
        files = [
            "travel_guidebook.py", "travel_state_app.py", "travel_agent.py", "travel_routing.py", "travel_patch.py",
            "contracts/travel_agent/v1/export.schema.json", "contracts/travel_agent/v1/reply.schema.json",
            "frontend/travel_workbench.html", "frontend/travel_workbench.js", "frontend/travel_workbench.css",
            "frontend/travel_result.js", "scripts/travel_agent/demo_m11.py", "scripts/travel_agent/smoke_m11.py",
            "scripts/travel_agent/verify_m11_pdf.py",
            "tests/travel_agent/m11/test_m11.py", "tests/travel_agent/m11/browser_checks.js", "tests/travel_agent/m11/fixture_m11.py",
        ]
        m02_files = {"m11_sha256": {file: digest(ROOT / file) for file in files}}
    write_report(output, cases, scope=scope, collected=collected, exit_code=exit_code,
                 snapshot_sha256=snapshot_sha, launch_cwd=str(launch_dir),
                 started_at=started, cwd=str(workdir), interpreter=sys.executable,
                 mode=args.mode, allow_network=False, allow_paid_calls=False, **m02_files)
    counts = {status: sum(case.status == status for case in cases) for status in ("PASS", "FAIL", "BLOCKED", "SKIPPED")}
    print(f"{scope}: {counts}; collected={collected}; exit={exit_code}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

