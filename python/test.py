from prisma import Prisma


async def test() -> None:
    client = Prisma()
    await client.connect()
    dataset = await client.dataset.find_first(where={"name": {"startswith": "x"}})
    if dataset:
        dataset.id
        dataset.name
