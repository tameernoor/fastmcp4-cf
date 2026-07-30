# Cloudflare Containers requires linux/amd64. The constant platform is
# deliberate — it cross-builds correctly from an arm64 Mac — so silence the
# lint that would otherwise flag it.
# check=skip=FromPlatformFlagConstDisallowed
FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# --pre is required: fastmcp 4 is a beta, and it resolves to fastmcp-slim==4.0.0b1.
RUN pip install --no-cache-dir --pre -r requirements.txt

COPY server.py .

# Must match `defaultPort` on the Container class in src/index.ts.
EXPOSE 8080

CMD ["python", "server.py"]
