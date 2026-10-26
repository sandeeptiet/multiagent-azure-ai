FROM python:3.14-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

COPY pyproject.toml requirements.lock ./
COPY claims_triage ./claims_triage
COPY data ./data
COPY knowledge ./knowledge
COPY app.py ./
COPY .streamlit/config.toml ./.streamlit/config.toml

RUN python -m pip install -r requirements.lock \
    && python -m pip install --no-deps -e .

EXPOSE 8501

CMD ["sh", "-c", "python -m claims_triage --backend azure provision && exec python -m streamlit run app.py --server.address=0.0.0.0 --server.port=8501 --server.headless=true"]