# Headers for all site responses. nginx does not inherit add_header if a location has its
# own, so the file is included in every location (include /etc/nginx/conf.d/security.inc).
# Not *.conf: nginx itself picks up only such files from conf.d, and this one is needed
# only inside server.
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
# HTTPS only (the browser remembers it for half a year; on a bare IP and over HTTP the
# header is ignored). Without includeSubDomains.
add_header Strict-Transport-Security "max-age=15552000" always;
# Other sites cannot embed our pages in frames (clickjacking). The landing page's own
# frames come from the same address.
add_header X-Frame-Options "SAMEORIGIN" always;
add_header Content-Security-Policy "frame-ancestors 'self'" always;
