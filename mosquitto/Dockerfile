FROM eclipse-mosquitto:2

COPY mosquitto.conf /mosquitto/config/mosquitto.conf
COPY entrypoint.sh /mosquitto/config/entrypoint.sh
RUN chmod +x /mosquitto/config/entrypoint.sh

ENTRYPOINT ["/mosquitto/config/entrypoint.sh"]
