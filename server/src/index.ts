import express from "express";
import type { Request, Response } from "express";
import cors from "cors";

const app = express();
const port = process.env.PORT || 2218;

// Middleware
app.use(cors());
app.use(express.json());

// Health check endpoint
app.get("/health", (_req: Request, res: Response) => {
  res.json({ status: "ok" });
});

// Start server
app.listen(port, () => {
  console.log(`Server is running on port ${port}`);
});
