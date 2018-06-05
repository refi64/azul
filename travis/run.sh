#!/usr/bin/bash

dnf install -y gnupg make openssl

openssl aes-256-cbc -K $encrypted_942ba5796743_key -iv $encrypted_942ba5796743_iv -in travis/flatpak.secret.gpg.enc -out flatpak.secret.gpg -d
openssl aes-256-cbc -K $encrypted_942ba5796743_key -iv $encrypted_942ba5796743_iv -in travis/flatpak.ssh.enc -out ~/.ssh/id_rsa -d
openssl aes-256-cbc -K $encrypted_942ba5796743_key -iv $encrypted_942ba5796743_iv -in travis/flatpak.ssh.pub.enc -out ~/.ssh/id_rsa.pub -d
gpg --import flatpak.secret.gpg

flatpak install flathub org.flatpak.Builder

# echo "$GPG_KEY" | base64 -d | gpg --import
# echo "$SSH_PUB_KEY" | base64 -d > ~/.ssh/id_rsa.pub
# echo "$SSH_KEY" | base64 -d > ~/.ssh/id_rsa

# git clone git@github.com:kirbyfan64/flatpak flatpak/repo
make flatpak FLATPAK_BUILDER='flatpak run org.flatpak.Builder'

#flatpak build-sign flatpak/repo --gpg-sign=F87AC6D0846D68FBAD17E313B129D657664A528A
#flatpak build-update-repo flatpak/repo --generate-static-deltas --gpg-sign=F87AC6D0846D68FBAD17E313B129D657664A528A
#cd flatpak/repo
#git add .
#git commit -am "deploy $1 on `date '+%Y-%m-%d-%H:%M:%S-UTC:%z'`"
#git push
