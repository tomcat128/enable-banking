FROM php:8.3-apache

# Keine Versionsangaben in den Antwort-Headern.
RUN echo "expose_php = Off" > /usr/local/etc/php/conf.d/hardening.ini

COPY apache.conf /etc/apache2/sites-available/000-default.conf
COPY callback.php /var/www/html/callback.php

EXPOSE 80
