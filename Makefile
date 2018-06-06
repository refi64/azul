FLATPAK_BUILDER=flatpak-builder
FLATPAK_REPO=repo
PYTHON=python3
INKSCAPE=inkscape
MAGICK=magick

.PHONY: flatpak sdist

# azul.yaml should be remade each time, just in case
all: flatpak flatpak/azul.yaml

flatpak/requirements.json:
	cat requirements.txt | xargs flatpak-pip-generator --output $@
	sed -i 's/pip3 install/pip3 install --no-build-isolation/;s/python3-attrs/requirements/' $@

sdist:
	rm -rf dist
	$(PYTHON) setup.py sdist

flatpak/azul.yaml: sdist
	echo 'name: azul' > $@
	echo 'buildsystem: simple' >> $@
	echo 'build-commands: [python3 setup.py install --prefix=/app]' >> $@
	echo 'sources:' >> $@
	echo '  - type: archive' >> $@
	echo '    path: ../'`echo dist/azul*.tar.gz` >> $@
	echo '    sha256: '`sha256sum dist/azul*.tar.gz | awk '{print $$1}'` >> $@

flatpak: flatpak/requirements.json flatpak/azul.yaml
	cd flatpak; $(FLATPAK_BUILDER) --repo=$(FLATPAK_REPO) --force-clean root com.refi64.Azul.yaml

icon:
	@rm -rf misc/icons
	@mkdir -p misc/icons
	$(INKSCAPE) -z misc/icon.svg -e misc/icons/full.png
	@set -e; for size in 16 22 24 32 48 64 128 256; do \
		radius=`expr $$size / 2`; \
		mkdir -p misc/icons/$${size}x$$size; \
		( \
			set -x; \
			$(MAGICK) misc/icons/full.png -resize $${size}x$${size} \
				\(  -size $${size}x$$size xc:none -fill white \
					-draw "circle $$radius,$$radius $$radius,0" -alpha copy \) \
				-compose copy_opacity -composite \
				misc/icons/$${size}x$$size/com.refi64.Azul.png \
		); \
	done
