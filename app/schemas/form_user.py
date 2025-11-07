from schemas.user_params import UserParams
from schemas.pid_value import PidValue
from schemas.filter_value import FilterValue
from pydantic import BaseModel

class FormUser(BaseModel):
    userParams: UserParams
    pidValue: PidValue
    filterValue: FilterValue