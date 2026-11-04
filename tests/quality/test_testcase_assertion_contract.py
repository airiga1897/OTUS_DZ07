"""A Terraform fixture helper must not replace unittest's assertion failure hook."""

from unittest.mock import patch

import pytest
import test_stage_failure_evidence as fixtures
from cloud_contract import TerraformError


def test_failed_assertion_raises_without_running_terraform() -> None:
    case = fixtures.StageFailureEvidenceTests()
    case.setUp()
    try:
        with (
            patch.object(case.store, "tf", side_effect=TerraformError("synthetic")) as terraform,
            pytest.raises(AssertionError, match="assertion contract"),
        ):
            case.fail("assertion contract")
        terraform.assert_not_called()
    finally:
        case.doCleanups()
