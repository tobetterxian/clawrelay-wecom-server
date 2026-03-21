from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.codex_cli_orchestrator import CodexCliOrchestrator
from src.core.project_deployment_manager import ProjectDeploymentManager
from src.core.wechat_miniprogram_manager import WeChatMiniProgramManager


def test_parse_wechat_miniprogram_second_phase_commands():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        request, usage = orchestrator._parse_deployment_command("启用小程序提审")
        assert usage is None
        assert request == {
            "action": "enable_wechat_miniprogram_audit",
            "config_path": "",
        }

        request, usage = orchestrator._parse_deployment_command(
            "提交微信小程序审核 .github/wechat-miniprogram-audit.json"
        )
        assert usage is None
        assert request == {
            "action": "submit_wechat_miniprogram_audit",
            "config_path": ".github/wechat-miniprogram-audit.json",
        }

        request, usage = orchestrator._parse_deployment_command("小程序审核状态 123456")
        assert usage is None
        assert request == {
            "action": "query_wechat_miniprogram_audit_status",
            "audit_id": "123456",
        }

        request, usage = orchestrator._parse_deployment_command("发布小程序")
        assert usage is None
        assert request == {"action": "release_wechat_miniprogram"}

        request, usage = orchestrator._parse_deployment_command("撤回微信小程序审核")
        assert usage is None
        assert request == {"action": "undo_wechat_miniprogram_audit"}


def test_scaffold_wechat_miniprogram_audit_config_writes_template():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        manager = ProjectDeploymentManager()
        result = manager.scaffold_wechat_miniprogram_audit_config(
            str(workspace),
            ".github/wechat-miniprogram-audit.json",
        )

        config_path = workspace / ".github" / "wechat-miniprogram-audit.json"
        content = config_path.read_text(encoding="utf-8")

        assert result.deployment_type == "wechat_miniprogram"
        assert result.config_path == ".github/wechat-miniprogram-audit.json"
        assert config_path.exists()
        assert '"item_list"' in content
        assert '"version_desc"' in content


def test_wechat_miniprogram_manager_submit_audit_requests_expected_endpoints():
    manager = WeChatMiniProgramManager()
    calls = []

    def fake_request_json(endpoint, method="GET", payload=None, query_params=None, access_token=""):
        calls.append(
            {
                "endpoint": endpoint,
                "method": method,
                "payload": payload,
                "query_params": query_params,
                "access_token": access_token,
            }
        )
        if endpoint == "/cgi-bin/stable_token":
            return {"access_token": "token_123", "expires_in": 7200}
        if endpoint == "/wxa/submit_audit":
            return {"errcode": 0, "errmsg": "ok", "auditid": 888}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    manager._request_json = fake_request_json
    result = manager.submit_audit(
        appid="wx1234567890ab",
        appsecret="secret_123",
        payload={"item_list": [{"address": "pages/index/index"}]},
    )

    assert result["auditid"] == 888
    assert calls[0]["endpoint"] == "/cgi-bin/stable_token"
    assert calls[0]["payload"]["appid"] == "wx1234567890ab"
    assert calls[1]["endpoint"] == "/wxa/submit_audit"
    assert calls[1]["access_token"] == "token_123"
    assert calls[1]["payload"] == {"item_list": [{"address": "pages/index/index"}]}


def test_wechat_miniprogram_deployment_summary_includes_audit_metadata():
    summary = ProjectDeploymentManager.deployment_summary(
        {
            "deployment_type": "wechat_miniprogram",
            "deployment_config": {
                "appid": "wx1234567890ab",
                "project_path": "miniprogram",
                "robot": 2,
                "audit_config_path": ".github/wechat-miniprogram-audit.json",
                "latest_audit_id": "888",
                "latest_audit_status": "submitted",
            },
        }
    )

    assert "微信小程序" in summary
    assert "audit_config=.github/wechat-miniprogram-audit.json" in summary
    assert "latest_audit_id=888" in summary
    assert "audit_status=submitted" in summary
