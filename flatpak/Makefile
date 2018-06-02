requirements.json: ../requirements.txt
	cat ../requirements.txt | grep -v 'pycairo\|pygobject' | xargs flatpak-pip-generator --output requirements.json
