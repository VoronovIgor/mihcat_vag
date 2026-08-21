FROM nginx:alpine
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
RUN mkdir -p /var/www/html/images/Imgs /var/www/html/images/Tnrpics
