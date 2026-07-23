# Snowflake MCP Server

Local MCP server for uploading files to Snowflake stages. Used with Claude Desktop.

## Setup

```bash
cd snowflake-mcp
uv venv
source .venv/bin/activate
uv add "mcp[cli]" snowflake-connector-python
```

## Configure

Copy the config template:
```bash
cp snowflake-mcp-config.example.json ~/.snowflake-mcp.json
```

Edit `~/.snowflake-mcp.json`:
```json
{
    "account": "YOUR_ACCOUNT.snowflakecomputing.com",
    "user": "YOUR_USER",
    "authenticator": "externalbrowser"
}
```

## Claude Desktop Config

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
    "mcpServers": {
        "snowflake": {
            "command": "uv",
            "args": [
                "--directory", "/ABSOLUTE/PATH/TO/snowflake-mcp",
                "run", "snowflake_mcp.py"
            ]
        }
    }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `put_file` | Upload local file to Snowflake stage |
| `list_stage` | List files on a stage |
| `remove_file` | Remove a file from a stage |
| `execute_sql` | Run SQL on Snowflake |
| `get_presigned_url` | Get pre-signed URL for a staged file |

## Usage

In Claude Desktop:
> Upload /Users/me/report.pdf to @DEV_DB.DNA.DOCS

> List all PDFs on @DEV_DB.DNA.DOCS

> Run SELECT * FROM DEV_DB.DNA.Q1_2024_CHUNKS LIMIT 5
