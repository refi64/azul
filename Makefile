.PHONY: flatpak sdist

all: flatpak

flatpak/requirements.json:
	cat requirements.txt | xargs flatpak-pip-generator --output $@
	sed -i 's/pip3 install/pip3 install --no-build-isolation/;s/python3-attrs/requirements/' $@

sdist:
	rm -rf dist
	python setup.py sdist

flatpak/azul.yaml: sdist
	echo 'name: azul' > $@
	echo 'buildsystem: simple' >> $@
	echo 'build-commands: [python3 setup.py install --prefix=/app]' >> $@
	echo 'sources:' >> $@
	echo '  - type: archive' >> $@
	echo '    path: ../'`echo dist/azul*.tar.gz` >> $@
	echo '    sha256: '`sha256sum dist/azul*.tar.gz | awk '{print $$1}'` >> $@

flatpak: flatpak/requirements.json flatpak/azul.yaml
	cd flatpak; flatpak-builder --repo=repo --force-clean root com.refi64.Azul.yaml
