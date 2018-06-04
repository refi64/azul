# Azul

A native, GTK+-powered [Zulip](https://zulipchat.com/) desktop client. Still a work in
progress, but it can already connect to servers, read and search messages, and send new
messages.

## Requirements

- Python 3.6.
- GTK+ 3.
- `glib-compile-schemas`.
- Everything in `requirements.txt`.

## Downloading

```
$ git clone https://github.com/kirbyfan64/azul
```

## Usage

### Via pip

```
$ pip install --user requirements.txt
$ sudo python3 setup.py install
# Run azul
$ azul
```

### Via flatpak

TODO

### Via flatpak (locally)

```
$ make flatpak
# Add your remote
$ flatpak remote-add --no-gpg-verify "$PWD/flatpak/repo"
# Install azul
$ flatpak install azul-local com.refi64.Azul
# Run azul
$ flatpak run com.refi64.azul
```
