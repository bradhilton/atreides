#!/bin/bash
gpg --decrypt secrets.tar.gpg > secrets.tar
(
    tar -xvf secrets.tar && 
    rm secrets.tar && 
    gcloud auth activate-service-account --key-file .gcloud.json
) & (
    cursor --install-extension ms-python.python &
    cursor --install-extension ms-python.black-formatter &
    cursor --install-extension ms-toolsai.jupyter &
    cursor --install-extension github.copilot
) & (
    sudo snap install --classic astral-uv &&
    uv sync
) & (
    git config --global user.name "Brad Hilton" && 
    git config --global user.email "brad.hilton.nw@gmail.com"
) & wait # Wait for all background processes to finish
