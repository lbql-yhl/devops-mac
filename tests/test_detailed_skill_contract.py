#!/usr/bin/env python3
from test_segmented_skill_command_contract import (
    test_every_mainline_skill_is_command_only_and_independently_runnable,
    test_every_skill_uses_one_existing_host_entry_script,
    test_exact_four_segments_and_order_are_project_contract,
    test_segment_handoffs_stop_at_each_segment_boundary,
    test_code_processing_segment_remains_out_of_scope,
)


def main() -> None:
    test_exact_four_segments_and_order_are_project_contract()
    test_every_mainline_skill_is_command_only_and_independently_runnable()
    test_every_skill_uses_one_existing_host_entry_script()
    test_segment_handoffs_stop_at_each_segment_boundary()
    test_code_processing_segment_remains_out_of_scope()
    print("DETAILED_SKILL_CONTRACT=verified")


if __name__ == "__main__":
    main()
