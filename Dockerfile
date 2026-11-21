FROM python:3.12-slim AS base
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir pip==26.2.2 && pip install --no-cache-dir -r requirements.txt
COPY . .

# Hard test gate: the final stage copies the marker from this stage, so a
# plain `docker compose build` cannot succeed with failing tests.
FROM base AS test
# The tick tests drive real git remotes and clones, so the test stage needs git.
# The final image doesn't: tick runs on the host.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pytest==8.3.4 httpx==0.28.1 && python -m pytest -q && touch /tests-passed

FROM base AS final
COPY --from=test /tests-passed /tests-passed
EXPOSE 8000
CMD ["uvicorn", "web:main_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
