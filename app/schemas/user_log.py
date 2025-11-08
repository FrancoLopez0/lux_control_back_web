from pydantic import BaseModel
from datetime import datetime

class UserLog(BaseModel):
    log:int
    lux:int
    time:str