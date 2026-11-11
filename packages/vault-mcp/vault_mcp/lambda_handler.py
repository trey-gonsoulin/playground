from mangum import Mangum

from vault_mcp.proxy import create_proxy_app

handler = Mangum(create_proxy_app(), lifespan="auto")
