FROM php:8.5-apache

# Keine Versionsangaben in den Antwort-Headern.
RUN echo "expose_php = Off" > /usr/local/etc/php/conf.d/hardening.ini

COPY conf/apache.conf /etc/apache2/sites-available/000-default.conf
COPY html/ /var/www/html/

EXPOSE 80
