"""Integration tests for device group tools."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nff_ota.models.group import CreateGroupParams, ModifyGroupMembersParams
from nff_ota.tools.device_groups.ops import (
    add_devices_to_group,
    create_group,
    list_groups,
    remove_devices_from_group,
)


def _group_row(group_id: str = "grp-1", name: str = "fleet") -> dict:
    return {
        "id": group_id,
        "name": name,
        "filter_criteria": {},
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _make_table_mock(call_responses: list[list[dict]]) -> MagicMock:
    """Returns a mock where each successive .execute() call returns the next response."""
    call_n = [0]
    def side_effect(name):
        t = MagicMock()
        t.select.return_value = t
        t.insert.return_value = t
        t.update.return_value = t
        t.delete.return_value = t
        t.eq.return_value = t
        t.in_.return_value = t
        t.order.return_value = t
        def execute():
            idx = call_n[0]
            call_n[0] += 1
            data = call_responses[idx] if idx < len(call_responses) else []
            return MagicMock(data=data, error=None)
        t.execute = execute
        return t
    return side_effect


class TestCreateGroup:
    async def test_creates_group(self, mock_supabase):
        row = _group_row()

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.insert.return_value = t
            t.eq.return_value = t
            t.execute.return_value = MagicMock(data=[row], error=None)
            return t
        mock_supabase.table.side_effect = table_se

        result = await create_group(CreateGroupParams(name="fleet"))
        assert result.error is None
        assert result.group is not None
        assert result.group.name == "fleet"
        assert result.group.member_count == 0


class TestAddRemoveDevices:
    async def test_add_devices(self, mock_supabase):
        group_row = _group_row()
        responses = [
            [group_row],   # select group
            [],            # insert members
            [{"device_id": "d1"}, {"device_id": "d2"}],  # count query
        ]
        mock_supabase.table.side_effect = _make_table_mock(responses)

        result = await add_devices_to_group(
            ModifyGroupMembersParams(group_id="grp-1", device_ids=["d1", "d2"])
        )
        assert result.error is None
        assert result.group.member_count == 2

    async def test_add_to_nonexistent_group_returns_error(self, mock_supabase):
        mock_supabase.table.side_effect = _make_table_mock([[]])  # group not found

        result = await add_devices_to_group(
            ModifyGroupMembersParams(group_id="no-such-group", device_ids=["d1"])
        )
        assert result.error is not None
        assert "not found" in result.error

    async def test_remove_devices(self, mock_supabase):
        group_row = _group_row()
        responses = [
            [group_row],  # select group
            [],           # delete members
            [],           # count (now empty)
        ]
        mock_supabase.table.side_effect = _make_table_mock(responses)

        result = await remove_devices_from_group(
            ModifyGroupMembersParams(group_id="grp-1", device_ids=["d1"])
        )
        assert result.error is None
        assert result.group.member_count == 0


class TestListGroups:
    async def test_list_empty(self, mock_supabase):
        mock_supabase.table.return_value.select.return_value.order.return_value.execute.return_value = MagicMock(data=[], error=None)
        result = await list_groups()
        assert result == []

    async def test_list_with_groups(self, mock_supabase):
        rows = [_group_row("g1"), _group_row("g2", "staging")]
        call_n = [0]

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.order.return_value = t
            t.eq.return_value = t

            def execute():
                call_n[0] += 1
                if call_n[0] == 1:
                    return MagicMock(data=rows, error=None)
                return MagicMock(data=[], error=None)  # member count queries
            t.execute = execute
            return t

        mock_supabase.table.side_effect = table_se
        result = await list_groups()
        assert len(result) == 2
