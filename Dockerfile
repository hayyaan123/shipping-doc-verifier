# Review console + pipeline. Runs anywhere Docker runs (Render, Koyeb, Cloud Run, your laptop). Listens on $PORT (default 7860).
FROM python:3.11-slim
RUN useradd -m -u 1000 user
USER user
ENV PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /home/user/app
COPY --chown=user requirements-app.txt .
RUN pip install --no-cache-dir -r requirements-app.txt
COPY --chown=user sdv ./sdv
COPY --chown=user demo ./demo
COPY --chown=user deploy/start.sh ./start.sh
# (a Windows checkout may have turned LF into CRLF; the shell script must be LF)
RUN sed -i 's/\r$//' start.sh && chmod +x start.sh && ./start.sh build
EXPOSE 7860
CMD ["./start.sh"]
