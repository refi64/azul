#!/usr/bin/bash

set -ex

cd /workspace

dnf install -y gnupg make openssl

openssl aes-256-cbc -K $encrypted_942ba5796743_key -iv $encrypted_942ba5796743_iv -in travis/flatpak.secret.gpg.enc -out flatpak.secret.gpg -d
openssl aes-256-cbc -K $encrypted_942ba5796743_key -iv $encrypted_942ba5796743_iv -in travis/flatpak.ssh.enc -out ~/.ssh/flatpak -d
openssl aes-256-cbc -K $encrypted_942ba5796743_key -iv $encrypted_942ba5796743_iv -in travis/flatpak.ssh.pub.enc -out ~/.ssh/flatpak.pub -d

gpg --import flatpak.secret.gpg
mv ~/.ssh/{flatpak,id_rsa}
mv ~/.ssh/{flatpak,id_rsa}.pub

flatpak install flathub org.flatpak.Builder

git clone git@github.com:kirbyfan64/flatpak flatpak-repo
mkdir -p flatpak-repo/dl
ln -s $PWD/flatpak-repo/dl flatpak/repo
make flatpak FLATPAK_BUILDER='flatpak run org.flatpak.Builder'

#flatpak build-sign flatpak/repo --gpg-sign=F87AC6D0846D68FBAD17E313B129D657664A528A
#flatpak build-update-repo flatpak/repo --generate-static-deltas --gpg-sign=F87AC6D0846D68FBAD17E313B129D657664A528A
#cd flatpak/repo
#git add .
#git commit -am "deploy $1 on `date '+%Y-%m-%d-%H:%M:%S-UTC:%z'`"
#git push
