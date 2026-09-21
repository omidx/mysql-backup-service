# Companion utility image for Docker deployments whose database image does not
# include mysqlbinlog (including Docker Official mysql:8.4 server-minimal).
FROM mysql:8.4
USER root
RUN microdnf install -y mysql-community-client \
    && microdnf clean all
