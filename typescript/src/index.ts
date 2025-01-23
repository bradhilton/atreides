import { PrismaClient } from '@prisma/client'

// Add any additional exports or SDK functionality here

export const prisma = new PrismaClient()
const result = await prisma.dataset.findMany()
console.log(result)
