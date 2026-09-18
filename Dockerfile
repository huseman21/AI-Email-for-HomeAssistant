ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

RUN apk add --no-cache python3

COPY app /app/app
COPY run.sh /app/run.sh
RUN chmod a+x /app/run.sh

# Invoke Python directly so startup does not depend on shell-script line
# endings or executable/shebang handling in the add-on runtime.
CMD ["python3", "/app/app/main.py"]
