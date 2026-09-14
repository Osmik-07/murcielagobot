"""
Заглушка. Актуально только если в будущем перейдёте на Full Mode
(Fragment cookies, например ради EVM-стейблкоинов) — тогда здесь
нужна периодическая переавторизация FragmentClient.
Пока используется No-Cookie режим (ton/usdt_ton), этот файл не используется.
"""

async def reauth_fragment_client(*args, **kwargs) -> None:
    pass
