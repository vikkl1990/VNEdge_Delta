from vnedge.research.external_repo_synthesis import build_external_repo_synthesis


def test_external_repo_synthesis_is_research_only_and_source_backed():
    payload = build_external_repo_synthesis()

    assert payload["synthesis_id"] == "external_repo_synthesis_20260731"
    assert payload["research_only"] is True
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["source_count"] >= 10
    assert payload["track_count"] >= 5

    repos = {source["repo"] for source in payload["sources"]}
    assert "Hitheshkaranth/OpenTerminalUI" in repos
    assert "OpenBB-finance/OpenBB" in repos
    assert "microsoft/qlib" in repos
    assert "ghostfolio/ghostfolio" in repos

    track_ids = {track["track_id"] for track in payload["build_tracks"]}
    assert "terminal_operator_shell_v1" in track_ids
    assert "agentic_research_os_v2" in track_ids
    assert "experiment_lineage_factory_v1" in track_ids
    assert "account_control_center_v1" in track_ids


def test_external_repo_synthesis_tracks_reference_known_sources():
    payload = build_external_repo_synthesis()
    repos = {source["repo"] for source in payload["sources"]}

    for track in payload["build_tracks"]:
        assert track["sources"], track["track_id"]
        assert all(source in repos for source in track["sources"])
        assert track["acceptance"], track["track_id"]
        assert "trade" in track["safety_boundary"].lower() or "read-only" in track[
            "safety_boundary"
        ].lower()
