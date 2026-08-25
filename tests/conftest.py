import os

# 必须在任何 `from app...` import 之前执行——app.main 在模块级调用
# get_settings()，如果 ENVIRONMENT 还是默认的 production，占位密钥会被
# config.py 的校验拒绝（这是有意为之的生产安全网，见 config.py），
# 测试环境显式声明自己是 development 来跳过这道校验。
os.environ.setdefault("ENVIRONMENT", "development")
