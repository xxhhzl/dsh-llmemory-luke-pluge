from pydantic import BaseModel, Field
from functools import wraps

import base
import frame

# 工具
tools = dict()

def tool(args_class, strict = True, additionalProperties = False):
    def decorator(func):
        schema = args_class.model_json_schema()
        # 去掉 pydantic 自动补的 title，只保留对模型有意义的约束
        properties = {
            name: {key: value for key, value in spec.items() if key != "title"}
            for name, spec in schema.get("properties", {}).items()
        }
        j = {
                "name": func.__name__,
                "description": func.__doc__.strip(),
                "strict": strict,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": schema.get("required", []),
                    "additionalProperties": additionalProperties
                }
            }
        tools.setdefault(func.__name__, (j, func))

        @wraps(func)
        def wr(*args, **kwargs):
            return func(*args, **kwargs)

        return wr
    return decorator



# 工具的定义
class memory_search_args(BaseModel):
    query: str = Field(description="检索语句")
    limit: int = Field(default=5, ge=1, le=20, description="返回条数")

@tool(memory_search_args)
def memory_search():
    '''
    查找记忆
    '''
    ...

# 获取工具的schema
def get_tools_schema(data, q: base.message_queue):
    frame.push(q, data, "tools.schema",
               [schema for (schema, func) in tools.values()],
               detail="消息队列已满，tools.schema 未能下发")


# 工具执行器
def tools_call(data):
    ...

