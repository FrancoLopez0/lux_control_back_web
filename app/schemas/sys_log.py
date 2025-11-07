from pydantic import BaseModel

class SysLog(BaseModel):
    log:str
    time:str