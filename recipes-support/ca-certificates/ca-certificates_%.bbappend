# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# Fetched from DigiCert's own publication point rather than vendored, so this
# layer does not store third-party certificate material. The certificate still
# ships in the image, so the redistribution question belongs to the full-build
# and deliverable review rather than to the repository source review.
#
# The "-1" suffix is load-bearing. DigiCert publishes a second, different
# certificate with the same subject CN at the unsuffixed URL; ours is serial
# 0CF5BD062B5602F47AB8502C23CCF066, the other is 085F94C02D857BE8CC14FF53EDA23E2A.
#
# The published form is DER, and the file is the certificate itself, so the
# sha256sum below is also its SHA-256 fingerprint. Pinning the download
# therefore pins the certificate identity, not merely the bytes of a wrapper.
DIGICERT_G2_CRT = "digicert-global-g2-tls-rsa-sha256-2020-ca1.crt"

# For the DER to PEM re-wrap in do_install.
DEPENDS += "openssl-native"

SRC_URI += "https://cacerts.digicert.com/DigiCertGlobalG2TLSRSASHA2562020CA1-1.crt;name=digicert-g2;downloadfilename=digicert-g2.der"
SRC_URI[digicert-g2.sha256sum] = "c8025f9fc65fdfc95b3ca8cc7867b9a587b5277973957917463fc813d0b625a9"

do_install:append() {
    install -d ${D}${datadir}/ca-certificates/extra

    # update-ca-certificates wants PEM and DigiCert publishes DER, so the
    # certificate is re-wrapped at install time. Verified to reproduce the
    # previously vendored PEM byte for byte.
    openssl x509 -inform DER -in ${WORKDIR}/digicert-g2.der \
        -outform PEM -out ${D}${datadir}/ca-certificates/extra/${DIGICERT_G2_CRT}
    chmod 0644 ${D}${datadir}/ca-certificates/extra/${DIGICERT_G2_CRT}

    if ! grep -qxF "extra/${DIGICERT_G2_CRT}" ${D}${sysconfdir}/ca-certificates.conf; then
        echo "extra/${DIGICERT_G2_CRT}" >> ${D}${sysconfdir}/ca-certificates.conf
    fi
}
