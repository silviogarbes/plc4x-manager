FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && \
    rm -rf /var/lib/apt/lists/*

ENV CMDSTAN=/opt/cmdstan

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    python -m cmdstanpy.install_cmdstan --dir /opt/cmdstan --cores 2

COPY *.py .

# Pre-compile numba JIT cache for stumpy (avoids 10+ min compilation at runtime)
ENV NUMBA_CACHE_DIR=/app/__pycache__
RUN python -c "import stumpy; import numpy as np; stumpy.stump(np.random.rand(200), m=20)" 2>/dev/null || true

RUN addgroup --system plc4x && adduser --system --ingroup plc4x plc4x
RUN chown -R plc4x:plc4x /opt/cmdstan
RUN chown -R plc4x:plc4x /app
USER plc4x

CMD ["python", "predictor.py"]
