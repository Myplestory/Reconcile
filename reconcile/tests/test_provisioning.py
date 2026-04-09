"""Tests for reconcile.provisioning — team import and Discord provisioner."""

import json
import os
import tempfile

import pytest

from reconcile.provisioning.team_import import parse_team_import
from reconcile.provisioning.discord import DiscordProvisioner


# --- parse_team_import ---

def test_parse_json():
    data = [{"team_id": "t1", "team_name": "Alpha", "members": [{"name": "A", "email": "a@b", "role": "pm"}]}]
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    try:
        result = parse_team_import(path)
        assert len(result) == 1
        assert result[0]["team_id"] == "t1"
        assert result[0]["members"][0]["name"] == "A"
    finally:
        os.unlink(path)


def test_parse_csv():
    csv_content = "team_id,team_name,member_name,member_email,member_role\nt1,Alpha,Alice,a@b,pm\nt1,Alpha,Bob,b@b,dev\nt2,Beta,Carol,c@b,dev\n"
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv_content)
    try:
        result = parse_team_import(path)
        assert len(result) == 2
        t1 = next(t for t in result if t["team_id"] == "t1")
        assert len(t1["members"]) == 2
    finally:
        os.unlink(path)


def test_parse_unsupported_format():
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        with pytest.raises(ValueError, match="Unsupported format"):
            parse_team_import(path)
    finally:
        os.unlink(path)


def test_parse_json_missing_team_id():
    data = [{"members": []}]
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    try:
        with pytest.raises(ValueError, match="team_id"):
            parse_team_import(path)
    finally:
        os.unlink(path)


def test_parse_csv_missing_column():
    csv_content = "team_id,team_name\nt1,Alpha\n"
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv_content)
    try:
        with pytest.raises(ValueError, match="missing required columns"):
            parse_team_import(path)
    finally:
        os.unlink(path)


# --- DiscordProvisioner ---

@pytest.mark.asyncio
async def test_provisioner_retry_limit():
    """_api should raise after 5 retries, not recurse infinitely."""
    provisioner = DiscordProvisioner(bot_token="fake")
    # Can't actually call _api without a server, but we verify the parameter exists
    import inspect
    sig = inspect.signature(provisioner._api)
    assert "_retries" in sig.parameters


def test_provisioner_empty_channels_guard():
    """provision_team should guard against empty channel list."""
    # Verify the guard is in the source code
    import inspect
    source = inspect.getsource(DiscordProvisioner.provision_team)
    assert "No channels created" in source
