from pydantic import BaseModel, Field


class CreateGroupParams(BaseModel):
    name: str = Field(min_length=1)
    filter_criteria: dict[str, str] = Field(default_factory=dict)


class ModifyGroupMembersParams(BaseModel):
    group_id: str = Field(min_length=1)
    device_ids: list[str] = Field(min_length=1)


class GroupInfo(BaseModel):
    id: str
    name: str
    filter_criteria: dict[str, str]
    member_count: int
    created_at: str


class GroupResult(BaseModel):
    group: GroupInfo | None = None
    error: str | None = None
