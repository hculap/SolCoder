from __future__ import annotations

from pathlib import Path
from types import MethodType, SimpleNamespace

from rich.console import Console

from solcoder.cli.app import CLIApp
import solcoder.cli.commands.metadata as md
from solcoder.session.manager import SessionManager
from solcoder.solana.wallet import WalletManager, WalletStatus
import solcoder.cli.storage as storage


def _make_app(tmp_path: Path) -> CLIApp:
    session_manager = SessionManager(root=tmp_path / "sessions")
    wallet_manager = WalletManager(keys_dir=tmp_path / "keys")
    console = Console(file=None, force_terminal=False)
    app = CLIApp(
        console=console,
        session_manager=session_manager,
        session_context=session_manager.start(),
        wallet_manager=wallet_manager,
    )
    # Non-interactive defaults
    app._prompt_text = lambda message: ""  # type: ignore[assignment]
    app._prompt_secret = lambda *args, **kwargs: "passphrase"  # type: ignore[assignment]
    return app


def test_metadata_set_writes_local_file(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    # Place a workspace
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "Anchor.toml").write_text("[workspace]\n")
    app.session_context.metadata.active_project = str(ws)
    app.session_manager.save(app.session_context)

    mint = "Mint11111111111111111111111111111111111111111"
    uri = "file:///tmp/metadata.json"
    resp = app.handle_line(f"/metadata set --mint {mint} --name Demo --symbol DMO --uri {uri}")
    assert any("Saved:" in msg for _, msg in resp.messages)
    out = ws / ".solcoder" / "metadata" / f"{mint}.json"
    assert out.exists()
    content = out.read_text()
    assert "\"name\": \"Demo\"" in content


def test_metadata_upload_ipfs(monkeypatch, tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    # Minimal config with IPFS key
    app.config_context = SimpleNamespace(config=SimpleNamespace(nft_storage_api_key="TEST_KEY", rpc_url="https://api.devnet.solana.com"))
    # Create a file to upload
    src = tmp_path / "metadata.json"
    src.write_text("{\"name\":\"X\"}")

    # Mock httpx.post
    class FakeResp:
        def __init__(self) -> None:
            self.status_code = 200

        def raise_for_status(self) -> None:  # pragma: no cover
            return

        def json(self) -> dict[str, object]:  # pragma: no cover
            return {"ok": True, "value": {"cid": "bafyTEST"}}

    monkeypatch.setattr(storage.httpx, "post", lambda *a, **k: FakeResp())

    resp = app.handle_line(f"/metadata upload --file {src} --storage ipfs")
    combined = "\n".join(m for _, m in resp.messages)
    assert "https://ipfs.io/ipfs/bafyTEST" in combined


def test_metadata_upload_bundlr(monkeypatch, tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    # Active workspace for runner location
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "Anchor.toml").write_text("[workspace]\n")
    app.session_context.metadata.active_project = str(ws)
    app.session_manager.save(app.session_context)

    # Create file
    src = tmp_path / "metadata.json"
    src.write_text("{\"name\":\"X\"}")

    # Mock bundlr upload
    monkeypatch.setattr(md, "bundlr_upload", lambda *a, **k: "https://arweave.net/FAKE")
    # Mock wallet
    ws_status = WalletStatus(exists=True, public_key="PUBKEY11111111111111111111111111111111", is_unlocked=False, wallet_path=tmp_path / "k.json")
    monkeypatch.setattr(app.wallet_manager, "status", lambda: ws_status)
    monkeypatch.setattr(app.wallet_manager, "export_wallet", lambda passphrase: "[1,2,3]")

    resp = app.handle_line(f"/metadata upload --file {src} --storage bundlr")
    combined = "\n".join(m for _, m in resp.messages)
    assert "https://arweave.net/FAKE" in combined


def test_metadata_set_run_invokes_runner(monkeypatch, tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    # Prepare workspace
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "Anchor.toml").write_text("[workspace]\n")
    app.session_context.metadata.active_project = str(ws)
    app.session_manager.save(app.session_context)

    # Capture runner invocation
    captured: dict[str, object] = {}

    monkeypatch.setattr(md, "_is_token2022_mint", lambda mint, rpc: True)

    def fake_spl(app_obj, mint, *, name, symbol, uri, rpc_url, metadata_path=None):
        captured["mint"] = mint
        captured["name"] = name
        captured["symbol"] = symbol
        captured["uri"] = uri
        captured["rpc_url"] = rpc_url
        if metadata_path:
            metadata_path.write_text('{"mint":"x"}')
        return True, ["Token-2022 metadata initialized on-chain via spl-token."], "https://arweave.net/token"

    monkeypatch.setattr(md, "_write_metadata_via_spl_token", fake_spl)

    mint = "Mint11111111111111111111111111111111111111111"
    uri = "file:///tmp/meta.json"
    resp = app.handle_line(f"/metadata set --mint {mint} --name Demo --symbol DMO --uri {uri} --run")

    combined = "\n".join(m for _, m in resp.messages)
    assert "Metadata set on-chain:" in combined
    assert "Token-2022 metadata initialized on-chain via spl-token." in combined
    assert "https://arweave.net/token" in combined
    assert "To write on-chain now:" not in combined
    assert captured["mint"] == mint
    assert captured["uri"] == uri


def test_metadata_set_run_aborts_when_bundlr_fails(monkeypatch, tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "Anchor.toml").write_text("[workspace]\n")
    app.session_context.metadata.active_project = str(ws)
    app.session_manager.save(app.session_context)

    monkeypatch.setattr(md, "_is_token2022_mint", lambda mint, rpc: True)
    monkeypatch.setattr(md.shutil, "which", lambda _binary: "/usr/bin/spl-token")

    def fail_bundlr(*args, **kwargs):
        raise RuntimeError("bundlr down")

    monkeypatch.setattr(md, "bundlr_upload", fail_bundlr)
    monkeypatch.setattr(md, "_write_metadata_via_spl_token", md._write_metadata_via_spl_token)
    status = WalletStatus(
        exists=True,
        public_key="Wallet111111111111111111111111111111111111111",
        is_unlocked=False,
        wallet_path=ws / "wallet.json",
    )
    status.wallet_path.write_text("[]")
    monkeypatch.setattr(app.wallet_manager, "status", lambda: status)
    monkeypatch.setattr(app.wallet_manager, "export_wallet", lambda passphrase: "[1,2,3]")

    resp = app.handle_line(
        "/metadata set --mint Mint11111111111111111111111111111111111111111 "
        "--name Demo --symbol DMO --uri file:///tmp/meta.json --run"
    )

    combined = "\n".join(m for _, m in resp.messages)
    assert "(warning) Bundlr upload failed: bundlr down" in combined
    assert "Aborting token-2022 metadata write because URI is still local." in combined
    assert "Token-2022 metadata initialized" not in combined


def test_metadata_set_run_falls_back_to_runner(monkeypatch, tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "Anchor.toml").write_text("[workspace]\n")
    app.session_context.metadata.active_project = str(ws)
    app.session_manager.save(app.session_context)

    monkeypatch.setattr(md, "_is_token2022_mint", lambda mint, rpc: False)

    def fake_spl(*args, **kwargs):
        raise AssertionError("Token-2022 path should not execute when mint is legacy" )

    monkeypatch.setattr(md, "_write_metadata_via_spl_token", fake_spl)

    runner_called: dict[str, object] = {}

    def fake_runner(app_obj, runner_dir, metadata_path, rpc_url):
        runner_called["dir"] = runner_dir
        runner_called["metadata_path"] = metadata_path
        runner_called["rpc_url"] = rpc_url
        return True, ["On-chain metadata write completed via Umi runner."]

    monkeypatch.setattr(md, "_run_metadata_via_node", fake_runner)

    mint = "Legacy111111111111111111111111111111111111111"
    uri = "https://example.com/meta.json"
    resp = app.handle_line(f"/metadata set --mint {mint} --name Legacy --symbol LGY --uri {uri} --run")

    combined = "\n".join(m for _, m in resp.messages)
    assert "Metadata set on-chain:" in combined
    assert "On-chain metadata write completed via Umi runner." in combined
    assert runner_called["dir"]
    assert Path(runner_called["metadata_path"]).exists()


def test_metadata_install_scaffolds_runners(monkeypatch, tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "Anchor.toml").write_text("[workspace]\n")
    app.session_context.metadata.active_project = str(ws)
    app.session_manager.save(app.session_context)

    set_runner = ws / ".solcoder" / "metadata_runner"
    bundlr_runner = ws / ".solcoder" / "bundlr_runner"
    ipfs_runner = ws / ".solcoder" / "ipfs_runner"
    for runner in (set_runner, bundlr_runner, ipfs_runner):
        (runner / "node_modules").mkdir(parents=True, exist_ok=True)
        (runner / "package.json").write_text("{}")

    monkeypatch.setattr(md, "_write_runner", lambda _app: set_runner)
    monkeypatch.setattr(storage, "ensure_bundlr_runner", lambda _root: bundlr_runner)
    monkeypatch.setattr(storage, "ensure_ipfs_runner", lambda _root: ipfs_runner)

    resp = app.handle_line("/metadata install")
    combined = "\n".join(message for _, message in resp.messages)
    assert "Prepared metadata runner" in combined
    assert "Prepared upload runners" in combined
    assert "Deps already installed" in combined
    assert "Unknown subcommand" not in combined


def test_metadata_wizard_uses_provided_defaults(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    app.session_context.metadata.active_project = str(ws)
    app.session_manager.save(app.session_context)

    prompts: list[str] = []

    original_dispatch = app.command_router.dispatch
    captured_set_lines: list[str] = []

    def _dispatch(self, app_obj, raw_line: str):
        if raw_line.startswith("metadata set"):
            captured_set_lines.append(raw_line)
        return original_dispatch(app_obj, raw_line)

    app.command_router.dispatch = MethodType(_dispatch, app.command_router)  # type: ignore[assignment]

    def _prompt_text(message: str) -> str:
        prompts.append(message)
        if message.startswith("Write metadata on-chain now?"):
            return "n"
        return ""

    app._prompt_text = _prompt_text  # type: ignore[assignment]

    mint = "Mint11111111111111111111111111111111111111111"
    resp = app.handle_line(
        f"/metadata wizard --mint {mint} --default-name YOLOcoin --default-symbol YOLO"
    )

    assert resp.messages
    assert any(prompt.startswith("Name [YOLOcoin]") for prompt in prompts)
    assert any(prompt.startswith("Symbol [YOLO]") for prompt in prompts)
    assert captured_set_lines, "metadata set dispatch was not invoked"
    set_line = captured_set_lines[0]
    assert "--name" in set_line and "YOLOcoin" in set_line
    assert "--symbol" in set_line and "YOLO" in set_line
    assert "--uri" in set_line
    assert "file://" in set_line


def test_bundlr_upload_invokes_runner(monkeypatch, tmp_path: Path) -> None:
    # Create dummy file to upload
    asset = tmp_path / "meta.json"
    asset.write_text("{\"name\": \"demo\"}")

    # Pretend npm/npx exist
    monkeypatch.setattr(storage.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd and cmd[0] == "npm":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd and cmd[0] == "npx":
            return SimpleNamespace(returncode=0, stdout="https://arweave.net/UPLOADED\n", stderr="")
        raise AssertionError(f"Unexpected command {cmd}")

    monkeypatch.setattr(storage.subprocess, "run", fake_run)

    url = storage.bundlr_upload(
        asset,
        rpc_url="https://api.devnet.solana.com",
        key_json="[1,2,3,4]",
        workspace_root=tmp_path,
    )

    assert url == "https://arweave.net/UPLOADED"
    assert len(calls) == 2
    npm_cmd, npm_kwargs = calls[0]
    assert npm_cmd == ["npm", "install"]
    assert npm_kwargs["cwd"] == str(tmp_path / ".solcoder" / "uploader_runner")
    npx_cmd, _ = calls[1]
    assert "--contentType" in npx_cmd
    idx = npx_cmd.index("--contentType")
    assert npx_cmd[idx + 1] == "application/json"
