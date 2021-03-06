#!/usr/bin/env python3
# Copyright 2021 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later

import glob
import pytest
import sys
import os

import add_pmbootstrap_to_import_path
import pmb.parse
import pmb.parse._apkbuild
import pmb.parse.apkindex
import pmb.helpers.repo


@pytest.fixture
def args(request):
    # Initialize args
    pmaports = os.path.realpath(f"{os.path.dirname(__file__)}/../..")
    sys.argv = ["pmbootstrap",
                "--aports", pmaports,
                "--log", "$WORK/log_testsuite_pmaports.txt"
                "chroot"]
    args = pmb.parse.arguments()

    # Initialize logging
    pmb.helpers.logging.init(args)
    request.addfinalizer(args.logfd.close)
    return args


def deviceinfo_obsolete(info):
    """
    Test for obsolete options used in the deviceinfo file. They must still be
    defined in pmbootstrap's config/__init__.py.
    """
    obsolete_options = ["weston_pixman_type"]
    for option in obsolete_options:
        if info[option]:
            raise RuntimeError("option '" + option + "' is obsolete, please"
                               " remove it (reasons for removal are at"
                               " <https://postmarketos.org/deviceinfo>)")


def test_deviceinfo(args):
    """
    Parse all deviceinfo files successfully and run checks on the parsed data.
    """
    # Iterate over all devices
    last_exception = None
    count = 0
    for folder in glob.glob(args.aports + "/device/*/device-*"):
        device = folder[len(args.aports):].split("-", 1)[1]

        try:
            # Successful deviceinfo parsing / obsolete options
            info = pmb.parse.deviceinfo(args, device)
            deviceinfo_obsolete(info)

            # deviceinfo_name must start with manufacturer
            name = info["name"]
            manufacturer = info["manufacturer"]
            if not name.startswith(manufacturer) and \
                    not name.startswith("Google"):
                raise RuntimeError("Please add the manufacturer in front of"
                                   " the deviceinfo_name, e.g.: '" +
                                   manufacturer + " " + name + "'")

        # Don't abort on first error
        except Exception as e:
            last_exception = e
            count += 1
            print(device + ": " + str(e))

    # Raise the last exception
    if last_exception:
        print("deviceinfo error count: " + str(count))
        raise last_exception


def device_dependency_check(apkbuild, path):
    """ Raise an error if a device package has a dependency that is not allowed
        (e.g. because it should be in a subpackage instead). """

    # asus-me176c: without this, the device will simply run the broken ACPI
    #              DSDT table, so we might as well update it. See pmaports!699.
    firmware_ok = {"device-asus-me176c": ["firmware-asus-me176c-acpi"]}

    pkgname = apkbuild["pkgname"]
    for depend in apkbuild["depends"]:
        if (depend.startswith("firmware-") or
                depend.startswith("linux-firmware")):
            if pkgname in firmware_ok and depend in firmware_ok[pkgname]:
                continue
            raise RuntimeError("Firmware package '" + depend + "' found in"
                               " depends of " + path + ". These go into"
                               " subpackages now, see"
                               " <https://postmarketos.org/devicepkg>.")


def test_aports_kernel(args):
    """
    Various tests performed on the /**/linux-* aports.
    """
    for path in glob.iglob(args.aports + "**/linux-*/APKBUILD", recursive=True):
        apkbuild = pmb.parse.apkbuild(args, path)

        if "pmb:cross-native" not in apkbuild["options"]:
            raise RuntimeError("\"pmb:cross-native\" missing in"
                               f" options= line: {path}")


def test_aports_device(args):
    """
    Various tests performed on the /device/*/device-* aports.
    """
    for path in glob.iglob(args.aports + "/device/*/device-*/APKBUILD"):
        apkbuild = pmb.parse.apkbuild(args, path)

        # Depends: Require "postmarketos-base"
        if "postmarketos-base" not in apkbuild["depends"]:
            raise RuntimeError("Missing 'postmarketos-base' in depends of " +
                               path)

        # Depends: Must not have firmware packages
        for depend in apkbuild["depends"]:
            device_dependency_check(apkbuild, path)

        # Architecture
        device = apkbuild["pkgname"][len("device-"):]
        deviceinfo = pmb.parse.deviceinfo(args, device)
        if "".join(apkbuild["arch"]) != deviceinfo["arch"]:
            raise RuntimeError("wrong architecture, please change to arch=\"" +
                               deviceinfo["arch"] + "\": " + path)
        if "!archcheck" not in apkbuild["options"]:
            raise RuntimeError("!archcheck missing in options= line: " + path)


def test_aports_device_kernel(args):
    """
    Verify the kernels specified in the device packages:
    * Kernel must not be in depends when kernels are in subpackages
    * Check if only one kernel is defined in depends
    """

    # Iterate over device aports
    for path in glob.glob(args.aports + "/device/*/device-*/APKBUILD"):
        # Parse apkbuild and kernels from subpackages
        apkbuild = pmb.parse.apkbuild(args, path)
        device = apkbuild["pkgname"][len("device-"):]
        kernels_subpackages = pmb.parse._apkbuild.kernels(args, device)

        # Parse kernels from depends
        kernels_depends = []
        for depend in apkbuild["depends"]:
            if not depend.startswith("linux-"):
                continue
            kernels_depends.append(depend)

            # Kernel in subpackages *and* depends
            if kernels_subpackages:
                raise RuntimeError("Kernel package '" + depend + "' needs to"
                                   " be removed when using kernel" +
                                   " subpackages: " + path)

        # No kernel
        if not kernels_depends and not kernels_subpackages:
            raise RuntimeError("Device doesn't have a kernel in depends or"
                               " subpackages: " + path)

        # Multiple kernels in depends
        if len(kernels_depends) > 1:
            raise RuntimeError("Please use kernel subpackages instead of"
                               " multiple kernels in depends (see"
                               " <https://postmarketos.org/devicepkg>): " +
                               path)


def parse_source_from_checksums(args, apkbuild_path):
    """
    Read the APKBUILD file and parse source files from the checksums at the
    bottom. This has always the same format, even if $source is built with hard
    to parse shell code (like in postmarketos-base).

    :param apkbuild_path: full path to the APKBUILD
    :returns: list of parsed "source" files, e.g.:
              ["first.patch", "second.patch"]
    """
    start = 'sha512sums="'
    in_block = False
    ret = []

    with open(apkbuild_path, encoding="utf-8") as handle:
        for line in handle.readlines():
            # Find start
            if not in_block:
                if line.startswith(start):
                    in_block = True
                else:
                    continue

            # sha512sums may have lines without checksums:
            # https://gitlab.alpinelinux.org/alpine/abuild/-/merge_requests/73
            if "  " not in line:
                continue

            try:
                _, filename = line.rstrip().split("  ", 2)
            except ValueError:
                raise ValueError("Failed to parse checksums. Try to delete the"
                                 " checksums and generate them again with"
                                 f" 'pmbootstrap checksum': {apkbuild_path}")

            # Find end
            if filename.endswith('"'):
                ret += [filename[:-1]]
                break

            ret += [filename]
    return ret


def test_aports_unreferenced_files(args):
    """
    Raise an error if an unreferenced file is found
    """
    for apkbuild_path in glob.iglob(args.aports + "/**/APKBUILD", recursive=True):
        apkbuild = pmb.parse.apkbuild(args, apkbuild_path)
        sources_chk = parse_source_from_checksums(args, apkbuild_path)

        # Collect install files from subpackages
        subpackage_installs = []
        if apkbuild["subpackages"]:
            for subpackage in apkbuild["subpackages"].values():
                if not subpackage:
                    continue
                try:
                    subpackage_installs += subpackage["install"]
                except KeyError:
                    continue

        # Collect trigger files
        trigger_sources = []
        if apkbuild["triggers"]:
            for trigger in apkbuild["triggers"]:
                trigger_sources.append(trigger.split("=")[0])

        dirname = os.path.dirname(apkbuild_path)
        for file in glob.iglob(dirname + "/**", recursive=True):
            rel_file_path = os.path.relpath(file, dirname)
            # Skip APKBUILDs and directories
            if rel_file_path == "APKBUILD" or os.path.isdir(file):
                continue

            if os.path.basename(rel_file_path) not in sources_chk \
                    and rel_file_path not in apkbuild["install"] \
                    and rel_file_path not in subpackage_installs \
                    and rel_file_path not in trigger_sources:
                raise RuntimeError(f"{apkbuild_path}: found unreferenced file: {rel_file_path}")


def test_aports_ui(args):
    """
    Raise an error if package in _pmb_recommends is not found
    """
    for arch in pmb.config.build_device_architectures:
        for path in glob.iglob(args.aports + "/main/postmarketos-ui-*/APKBUILD"):
            apkbuild = pmb.parse.apkbuild(args, path)
            # Skip if arch isn't enabled
            if not pmb.helpers.package.check_arch(args, apkbuild["pkgname"], arch, False):
                continue

            for package in apkbuild["_pmb_recommends"]:
                depend = pmb.helpers.package.get(args, package,
                                                 arch, must_exist=False)
                if depend is None or not pmb.helpers.package.check_arch(args, package, arch):
                    raise RuntimeError(f"{path}: package '{package}' from"
                                       f" _pmb_recommends not found for arch '{arch}'")

            # Check packages from "_pmb_recommends" of -extras subpackage if one exists
            if f"{apkbuild['pkgname']}-extras" in apkbuild["subpackages"]:
                apkbuild = apkbuild["subpackages"][f"{apkbuild['pkgname']}-extras"]
                for package in apkbuild["_pmb_recommends"]:
                    depend = pmb.helpers.package.get(args, package,
                                                     arch, must_exist=False)
                    if depend is None or not pmb.helpers.package.check_arch(args, package, arch):
                        raise RuntimeError(f"{path}: package '{package}' from _pmb_recommends "
                                           f"of -extras subpackage is not found for arch '{arch}'")
