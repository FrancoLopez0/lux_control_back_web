from pydantic import BaseModel

class FilterValue(BaseModel):
    r:float
    q:float
    alpha:float