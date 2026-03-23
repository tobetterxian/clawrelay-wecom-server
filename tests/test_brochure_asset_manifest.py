import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.core.codex_cli_orchestrator import CodexCliOrchestrator
from src.utils.brochure_asset_manifest import (
    load_brochure_asset_manifest,
    manifest_path_for_workspace,
    summarize_brochure_asset_manifest,
    write_brochure_asset_manifest,
)
from src.utils.brochure_canva_state import (
    canva_state_path_for_workspace,
    load_canva_brochure_state,
    summarize_canva_brochure_state,
    write_canva_brochure_state,
)
from src.utils.quoted_requirement_doc import parse_quoted_requirement_doc_request


def test_brochure_asset_manifest_roundtrip():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        payload = {
            "version": 1,
            "provider": "cloudinary",
            "generated_at": "2026-03-23T12:00:00+00:00",
            "asset_count": 1,
            "assets": [{"source_file": "brochure/assets/cover.png"}],
        }
        manifest_path = write_brochure_asset_manifest(str(workspace), payload)

        assert manifest_path == manifest_path_for_workspace(str(workspace))
        loaded = load_brochure_asset_manifest(str(workspace))
        assert loaded == payload
        summary = summarize_brochure_asset_manifest(loaded)
        assert "素材来源：cloudinary" in summary
        assert "素材数量：1" in summary


def test_handle_control_command_syncs_brochure_assets():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"project_id": "proj_1", "name": "hello-brochure"},
            },
            None,
        )
        captured = {}

        def fake_sync_workspace_assets(workspace_path: str, project_name: str = ""):
            captured["workspace_path"] = workspace_path
            captured["project_name"] = project_name
            return SimpleNamespace(
                manifest_relative_path="docs/brochure-assets.json",
                asset_count=3,
                source_count=3,
            )

        orchestrator.cloudinary_asset_manager = SimpleNamespace(
            is_enabled=lambda: True,
            sync_workspace_assets=fake_sync_workspace_assets,
        )

        reply = asyncio.run(
            orchestrator.handle_control_command(
                user_id="alice",
                content="同步画册素材到Cloudinary",
                session_key="alice",
                log_context={},
            )
        )

        assert captured["workspace_path"] == str(working_dir)
        assert captured["project_name"] == "hello-brochure"
        assert "已同步画册素材到 Cloudinary" in reply
        assert "docs/brochure-assets.json" in reply
        assert "下一步：可直接发送 `生成画册`" in reply


def test_handle_control_command_reports_brochure_asset_status():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"project_id": "proj_1", "name": "hello-brochure"},
            },
            None,
        )
        orchestrator.cloudinary_asset_manager = SimpleNamespace(
            load_manifest=lambda workspace_path: {
                "provider": "cloudinary",
                "asset_count": 2,
                "generated_at": "2026-03-23T12:00:00+00:00",
                "assets": [{"source_file": "brochure/assets/cover.png"}],
            }
        )

        reply = asyncio.run(
            orchestrator.handle_control_command(
                user_id="alice",
                content="查看画册素材状态",
                session_key="alice",
                log_context={},
            )
        )

        assert "当前画册素材状态" in reply
        assert "Manifest：docs/brochure-assets.json" in reply
        assert "素材来源：cloudinary" in reply
        assert "素材数量：2" in reply


def test_canva_brochure_state_roundtrip():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        payload = {
            "version": 1,
            "provider": "canva",
            "generated_at": "2026-03-23T12:00:00+00:00",
            "design_id": "DAG123",
            "design_title": "Hello Brochure",
            "page_count": 6,
        }
        state_path = write_canva_brochure_state(str(workspace), payload)

        assert state_path == canva_state_path_for_workspace(str(workspace))
        loaded = load_canva_brochure_state(str(workspace))
        assert loaded == payload
        summary = summarize_canva_brochure_state(loaded)
        assert "设计标题：Hello Brochure" in summary
        assert "设计ID：DAG123" in summary


def test_generate_canva_command_not_parsed_as_requirement_doc():
    assert parse_quoted_requirement_doc_request("生成Canva精修版 hello brochure") is None


def test_handle_control_command_generates_canva_brochure():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"project_id": "proj_1", "name": "hello-brochure"},
            },
            None,
        )
        captured = {}

        def fake_generate_polished_brochure(workspace_path: str, project_name: str = "", design_title: str = ""):
            captured["workspace_path"] = workspace_path
            captured["project_name"] = project_name
            captured["design_title"] = design_title
            return SimpleNamespace(
                state_relative_path="docs/canva-brochure.json",
                design_id="DAG123",
                edit_url="https://www.canva.com/design/DAG123/edit",
                view_url="https://www.canva.com/design/DAG123/view",
                design_title="Hello Brochure",
                page_count=6,
                dataset_field_count=8,
                autofill_field_count=6,
                asset_upload_count=3,
            )

        orchestrator.canva_design_manager = SimpleNamespace(
            is_enabled=lambda: True,
            generate_polished_brochure=fake_generate_polished_brochure,
        )

        reply = asyncio.run(
            orchestrator.handle_control_command(
                user_id="alice",
                content="生成Canva精修版 hello brochure",
                session_key="alice",
                log_context={},
            )
        )

        assert captured["workspace_path"] == str(working_dir)
        assert captured["project_name"] == "hello-brochure"
        assert captured["design_title"] == "hello brochure"
        assert "已生成 Canva 精修版" in reply
        assert "docs/canva-brochure.json" in reply
        assert "下一步：可直接发送 `获取Canva编辑链接` 或 `导出Canva画册PDF`" in reply


def test_handle_control_command_reports_canva_link():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"project_id": "proj_1", "name": "hello-brochure"},
            },
            None,
        )
        orchestrator.canva_design_manager = SimpleNamespace(
            load_state=lambda workspace_path: {
                "design_id": "DAG123",
                "design_title": "Hello Brochure",
                "generated_at": "2026-03-23T12:00:00+00:00",
                "page_count": 6,
                "edit_url": "https://www.canva.com/design/DAG123/edit",
                "view_url": "https://www.canva.com/design/DAG123/view",
            }
        )

        reply = asyncio.run(
            orchestrator.handle_control_command(
                user_id="alice",
                content="获取Canva编辑链接",
                session_key="alice",
                log_context={},
            )
        )

        assert "当前 Canva 画册状态" in reply
        assert "状态文件：docs/canva-brochure.json" in reply
        assert "设计ID：DAG123" in reply
        assert "编辑链接：https://www.canva.com/design/DAG123/edit" in reply


def test_handle_control_command_exports_canva_pdf():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"project_id": "proj_1", "name": "hello-brochure"},
            },
            None,
        )
        captured = {}

        def fake_export_design_pdf(workspace_path: str, output_path: str = ""):
            captured["workspace_path"] = workspace_path
            captured["output_path"] = output_path
            return SimpleNamespace(
                state_relative_path="docs/canva-brochure.json",
                design_id="DAG123",
                output_relative_path="dist/canva-brochure.pdf",
                output_path=str(working_dir / "dist" / "canva-brochure.pdf"),
                export_job_id="JOB123",
            )

        orchestrator.canva_design_manager = SimpleNamespace(
            is_enabled=lambda: True,
            export_design_pdf=fake_export_design_pdf,
        )

        reply = asyncio.run(
            orchestrator.handle_control_command(
                user_id="alice",
                content="导出Canva画册PDF dist/out.pdf",
                session_key="alice",
                log_context={},
            )
        )

        assert captured["workspace_path"] == str(working_dir)
        assert captured["output_path"] == "dist/out.pdf"
        assert "已导出 Canva 画册 PDF" in reply
        assert "输出文件：dist/canva-brochure.pdf" in reply
