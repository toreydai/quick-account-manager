from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health():
    """ALB TargetGroup 健康检查用（docs/design.md 3.1 节）。不检查下游 Keycloak
    连通性——这个端点只证明"这个进程还活着"，Keycloak 挂了不该导致 ALB 把这台
    实例标记不健康并摘掉（那样管理员反而更难登进来看发生了什么）。
    """
    return {"status": "ok"}
