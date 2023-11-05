#!/usr/bin/env python3
import base64
from fnmatch import fnmatch
import io
import os
import sys
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
import requests
import yaml


def urlquote(s, safe=""):
    return urllib.parse(s.encode("utf-8"), safe=safe)


def request(url, method="get"):
    (netrc_user, netrc_pass) = requests.utils.get_netrc_auth(url)
    netrc_b64 = base64.b64encode(f"{netrc_user}:{netrc_pass}".encode("utf-8")).decode("us-ascii")
    headers = {
        "Authorization": f"Basic {netrc_b64}",
        "Accept": "application/vnd.github.v3+json",
    }

    return requests.request(
        method,
        url,
        headers=headers,
    )


def main():
    # load configuration
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} CONFIG", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not config.get("allow_root", False) and os.geteuid() == 0:
        print("you aren't supposed to run this as root", file=sys.stderr)
        sys.exit(1)

    # ask for the password now already
    subprocess.run(["sudo", "-v"])

    # short output to differentiate between success and failure-cooldown
    print("here we go", file=sys.stderr)

    # check what artifacts we got
    response = request(f"https://api.github.com/repos/{config['repo']}/actions/artifacts")
    resp_json = response.json()
    artifact_count = resp_json["total_count"]
    all_artifacts = resp_json["artifacts"]

    page = 2
    while len(all_artifacts) < artifact_count:
        # but wait, there's more!
        response = request(f"https://api.github.com/repos/{config['repo']}/actions/artifacts?page={page}")
        resp_json = response.json()
        all_artifacts.extend(resp_json["artifacts"])
        page += 1

    # filter and sort
    want_branch = config.get("branch", "")
    wanted_artifacts = [
        art for art in all_artifacts
        if (
            art["name"] == config["artifact"]
            and (
                not want_branch
                or art["workflow_run"]["head_branch"] == want_branch
            )
        )
    ]
    wanted_artifacts.sort(key=lambda art: art["updated_at"], reverse=True)

    # get the freshest of them all
    response = request(f"https://api.github.com/repos/{config['repo']}/actions/artifacts/{wanted_artifacts[0]['id']}/zip")
    response_zip_bytes = response.content
    zip_location = config.get("zip-location", None)
    if zip_location is not None:
        with open(zip_location, "wb") as f:
            f.write(response_zip_bytes)

    response_zip_io = io.BytesIO(response_zip_bytes)
    response_zip = zipfile.ZipFile(response_zip_io)

    temp_to_target: dict[str, str] = {}

    # extract files
    for file_config in config.get("copy-files", []):
        binary_file = response_zip.open(file_config["archive-path"])

        with tempfile.NamedTemporaryFile(delete=False) as ntf:
            ntf_name = ntf.name

            while True:
                buf = binary_file.read(4*1024*1024)
                if not buf:
                    break
                ntf.write(buf)
            ntf.close()

            temp_to_target[ntf_name] = file_config["target-path"]

    # extract files by pattern
    for pattern_config in config.get("copy-patterns", []):
        pattern = pattern_config["pattern"]

        for zip_entry in response_zip.infolist():
            if zip_entry.is_dir():
                continue

            if not fnmatch(zip_entry.filename, pattern):
                continue

            binary_file = response_zip.open(zip_entry.filename)

            with tempfile.NamedTemporaryFile(delete=False) as ntf:
                ntf_name = ntf.name

                while True:
                    buf = binary_file.read(4*1024*1024)
                    if not buf:
                        break
                    ntf.write(buf)
                ntf.close()

                target_path = os.path.join(pattern_config["target-dir"], os.path.basename(zip_entry.filename))
                temp_to_target[ntf_name] = target_path

    systemd_services = config.get("systemd-services", [])
    for service in systemd_services:
        # stop service
        subprocess.run(["sudo", "systemctl", "stop", service])

    # deploy!
    for (ntf_name, target_path) in temp_to_target.items():
        subprocess.run(["sudo", "cp", ntf_name, target_path])

    for service in systemd_services:
        # start service
        subprocess.run(["sudo", "systemctl", "start", service])

    # clean up files
    for ntf_name in temp_to_target.keys():
        os.unlink(ntf_name)

    if systemd_services:
        time.sleep(1.5)
        for service in systemd_services:
            subprocess.run(["sudo", "env", "PAGER=", "systemctl", "status", service])


if __name__ == "__main__":
    main()
