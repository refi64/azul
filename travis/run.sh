#!/usr/bin/bash

set -ex

cd /workspace

dnf install -y gnupg make openssl
dnf update -y flatpak-builder

mkdir -p ~/.ssh
ssh-keyscan github.com >> ~/.ssh/known_hosts

openssl aes-256-cbc -K $encrypted_942ba5796743_key -iv $encrypted_942ba5796743_iv -in travis/secrets.tar.enc -out secrets.tar -d
tar xf secrets.tar

gpg --import secret.gpg
mv id_rsa* ~/.ssh

git clone git@github.com:kirbyfan64/flatpak flatpak-repo
mkdir -p flatpak-repo/dl
make flatpak FLATPAK_REPO=$PWD/flatpak-repo/dl

flatpak build-sign flatpak-repo/dl --gpg-sign=FB0070002D5809AD482B945836B3ECB2E3A22E51
flatpak build-update-repo flatpak-repo/dl --generate-static-deltas --gpg-sign=FB0070002D5809AD482B945836B3ECB2E3A22E51
cd flatpak-repo/
git add .
git commit -am "deploy $1 on `date '+%Y-%m-%d-%H:%M:%S-UTC:%z'`"
git push
