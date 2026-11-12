"""Rocket propulsion analysis helpers."""

from rocket_analysis.models import MachStation, RPA4Inputs, RPA4Reference
from rocket_analysis.rpa4 import (
    extract_report,
    load_reference_json,
    mach_stations_from_report_values,
    parse_report_values,
)
from rocket_analysis.rpa4_validation import (
    load_rpa4_config,
    load_rpa4_inputs,
    validate_rpa4_case,
)
from rocket_analysis.shared import SharedRPA4Case, load_shared_rpa4_cases
from rocket_analysis.optimization import (
    RPA4OptimizationResult,
    optimize_rpa4_cases,
)
from rocket_analysis.ranking import (
    RankingResult,
    candidate_metric_catalog,
    rank_metric_rows,
    report_metric_catalog,
)
from rocket_analysis.study import (
    GeneratedCandidate,
    ParameterSpec,
    StudySpec,
    design_constraint_violations,
    generate_candidate_cfgs,
    import_shared_rpa4_results,
    load_study_spec,
    rank_screened_candidates,
    rank_study_results,
    preview_parameter_sets,
    sample_parameter_sets,
    save_study_ranking,
    screen_study_candidates,
    stage_rpa4_candidates,
)
from rocket_analysis.thermochemistry import (
    CanteraEquilibriumScreen,
    CEAIdealScreen,
    run_cantera_equilibrium_screen,
    run_cea_ideal_screen,
)
from rocket_analysis.openmdao_adapter import build_callable_problem, evaluate_callable
from rocket_analysis.cfd import (
    openfoam_finalist_ids,
    prepare_openfoam_finalist_case,
    run_openfoam_finalist_case,
)
from rocket_analysis.bundles import (
    ReturnScanResult,
    WorkflowProblem,
    pack_study_candidates,
    scan_returns,
    unpack_rpa4_bundle,
)
from rocket_analysis.service import (
    CandidateStatus,
    CandidateStatusRow,
    JobManager,
    JobState,
    StudyService,
    abandon_adaptive_trials,
    candidate_statuses,
    create_initial_study,
    mark_candidate_stale,
    run_doctor,
)

__all__ = [
    "MachStation",
    "RPA4Reference",
    "RPA4Inputs",
    "extract_report",
    "load_reference_json",
    "parse_report_values",
    "mach_stations_from_report_values",
    "load_rpa4_config",
    "load_rpa4_inputs",
    "validate_rpa4_case",
    "SharedRPA4Case",
    "load_shared_rpa4_cases",
    "RPA4OptimizationResult",
    "optimize_rpa4_cases",
    "RankingResult",
    "report_metric_catalog",
    "candidate_metric_catalog",
    "rank_metric_rows",
    "ParameterSpec",
    "StudySpec",
    "design_constraint_violations",
    "GeneratedCandidate",
    "load_study_spec",
    "sample_parameter_sets",
    "preview_parameter_sets",
    "generate_candidate_cfgs",
    "screen_study_candidates",
    "stage_rpa4_candidates",
    "import_shared_rpa4_results",
    "rank_study_results",
    "rank_screened_candidates",
    "save_study_ranking",
    "CEAIdealScreen",
    "CanteraEquilibriumScreen",
    "run_cea_ideal_screen",
    "run_cantera_equilibrium_screen",
    "build_callable_problem",
    "evaluate_callable",
    "openfoam_finalist_ids",
    "prepare_openfoam_finalist_case",
    "run_openfoam_finalist_case",
    "WorkflowProblem",
    "ReturnScanResult",
    "pack_study_candidates",
    "unpack_rpa4_bundle",
    "scan_returns",
    "CandidateStatus",
    "CandidateStatusRow",
    "JobState",
    "JobManager",
    "StudyService",
    "candidate_statuses",
    "mark_candidate_stale",
    "abandon_adaptive_trials",
    "run_doctor",
    "create_initial_study",
]
