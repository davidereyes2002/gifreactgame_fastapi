# app/db.py
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

async def connect_db():
    return await asyncpg.connect(DATABASE_URL)

# Run a query and return a single row
async def fetchrow(query, *args):
    conn = await connect_db()
    try:
        row = await conn.fetchrow(query, *args)
        return row
    finally:
        await conn.close()

# Run a query and return multiple rows
async def fetch(query, *args):
    conn = await connect_db()
    try:
        rows = await conn.fetch(query, *args)
        return rows
    finally:
        await conn.close()

# Run a query that modifies data (INSERT, UPDATE, DELETE) and optionally returns rows
async def execute(query, *args):
    conn = await connect_db()
    try:
        if "returning" in query.lower():
            # If the query has a RETURNING clause, fetch and return the result
            rows = await conn.fetch(query, *args)
            return rows
        else:
            await conn.execute(query, *args)
            return None
    finally:
        await conn.close()

