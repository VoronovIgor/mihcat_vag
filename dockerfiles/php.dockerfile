FROM php:8.1-fpm-alpine

# Install mysqli extension (required by VAG catalog)
RUN docker-php-ext-install mysqli

# GD for getimagesize() on schema images
RUN apk add --no-cache freetype-dev libjpeg-turbo-dev libpng-dev \
    && docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-install gd

# Raise limits for large result sets and file uploads
RUN echo "memory_limit=512M" > /usr/local/etc/php/conf.d/custom.ini \
    && echo "max_execution_time=300" >> /usr/local/etc/php/conf.d/custom.ini \
    && echo "upload_max_filesize=512M" >> /usr/local/etc/php/conf.d/custom.ini \
    && echo "post_max_size=512M" >> /usr/local/etc/php/conf.d/custom.ini

WORKDIR /var/www/html
RUN mkdir -p images/Imgs images/Tnrpics
