from prisma import Prisma


async def test() -> None:
    client = Prisma()
    await client.connect()
    model = await client.model.find_first(where={"name": {"startswith": "x"}})
    if model:
        model.id
        model.name
