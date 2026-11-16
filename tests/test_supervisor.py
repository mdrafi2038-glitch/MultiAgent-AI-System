from app.supervisor import build_supervisor_plan, detect_integrations, validate_plan


def test_detects_github_and_render_pipeline():
    found = detect_integrations("Fix my GitHub repository, run tests, then deploy to Render")
    assert found == {"github", "render"}


def test_pipeline_orders_render_after_merge_and_requires_approval():
    plan = build_supervisor_plan("Update GitHub repo and deploy to Render")
    assert validate_plan(plan)
    by_id = {step["id"]: step for step in plan["steps"]}
    assert by_id["render_preflight"]["depends_on"] == ["merge"]
    assert by_id["merge"]["approval_required"] is True
    assert by_id["render_deploy"]["approval_required"] is True


def test_text_only_plan_has_review_dependency():
    plan = build_supervisor_plan("Write a short research report", ["research", "writer"])
    assert validate_plan(plan)
    review = next(step for step in plan["steps"] if step["id"] == "final_review")
    assert review["depends_on"] == ["specialist_1", "specialist_2"]


def test_invalid_dependency_cycle_is_rejected():
    plan = build_supervisor_plan("Research")
    plan["steps"][0]["depends_on"] = ["final_review"]
    assert not validate_plan(plan)


def test_render_inspection_does_not_schedule_a_deployment():
    plan = build_supervisor_plan("Check Render service and latest deploy health")
    assert validate_plan(plan)
    ids = [step["id"] for step in plan["steps"]]
    assert ids == ["render_preflight"]
    assert plan["steps"][0]["capability"] == "service_read"


def test_explicit_render_deploy_adds_approval_gated_step():
    plan = build_supervisor_plan("Check Render then deploy to Render")
    assert validate_plan(plan)
    by_id = {step["id"]: step for step in plan["steps"]}
    assert by_id["render_deploy"]["depends_on"] == ["render_preflight"]
    assert by_id["render_deploy"]["approval_required"] is True


def test_github_read_only_does_not_schedule_writes():
    plan = build_supervisor_plan("Inspect my GitHub repository and latest workflow")
    ids = [step["id"] for step in plan["steps"]]
    assert ids == ["github_inspect"]
