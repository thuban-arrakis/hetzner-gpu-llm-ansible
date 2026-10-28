"""Security boundaries for the explicitly requested remote ARM deployment runner."""

import yaml

from conftest import ROOT


def workflow(name):
    # BaseLoader preserves GitHub's `on` key instead of treating it as YAML 1.1 true.
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_management_runner_accepts_only_explicit_main_branch_deployment():
    deploy = workflow("deploy.yml")
    assert set(deploy["on"]) == {"workflow_dispatch"}
    job = deploy["jobs"]["deploy"]
    assert "github.event_name == 'workflow_dispatch'" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert job["runs-on"] == ["self-hosted", "linux", "ARM64", "llm-deploy"]
    assert deploy["permissions"] == {"contents": "read"}
    assert job["environment"] == "gpu-production"
    # PR validation must never run on the credential-capable management runner.
    validation = workflow("validate.yml")
    assert "pull_request" in validation["on"]
    assert all("self-hosted" not in job["runs-on"] for job in validation["jobs"].values())


def test_controller_avoids_home_state_and_emits_only_success_metadata():
    job = workflow("deploy.yml")["jobs"]["deploy"]
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] == "false"
    assert checkout["with"]["set-safe-directory"] == "false"
    assert job["env"]["PIP_NO_CACHE_DIR"] == "1"
    for key in ("PIP_CACHE_DIR", "PYTHONPYCACHEPREFIX", "XDG_CACHE_HOME", "TMPDIR"):
        assert job["env"][key].startswith("${{ runner.temp }}")
    deployment = next(step for step in job["steps"] if "ansible-playbook" in step.get("run", ""))
    assert "pip install --no-cache-dir" in deployment["run"]
    assert "ANSIBLE_HOME=\"$RUNNER_TEMP/" in deployment["run"]
    evidence = job["steps"][-1]
    assert evidence["if"] == "success()"
    assert set(evidence["env"]) == {"DEPLOYED_SHA", "DEPLOY_RUN_URL"}
    assert evidence["env"]["DEPLOYED_SHA"] == "${{ github.sha }}"
    assert "github.run_id" in evidence["env"]["DEPLOY_RUN_URL"]
    assert "$GITHUB_STEP_SUMMARY" in evidence["run"]
    assert "${{" not in evidence["run"]  # GitHub metadata is passed as quoted data.
