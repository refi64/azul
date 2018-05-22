DESTDIR=
PREFIX=/usr/share

SCHEMAS=$(DESTDIR)/$(PREFIX)/glib-2.0/schemas

compile-settings:
	glib-compile-schemas misc/

install-settings:
	install -Dm 644 misc/com.refi64.azul.gschema.xml $(SCHEMAS)
	glib-compile-schemas $(SCHEMAS)
