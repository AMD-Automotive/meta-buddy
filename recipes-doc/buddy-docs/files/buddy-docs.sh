# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# Point interactive logins (and any agent driving one) at the board docs.
# Guarded on an interactive shell so scp/sftp and non-interactive ssh stay clean.
case $- in
    *i*) ;;
    *) return 0 ;;
esac

printf '%s\n' \
    "RAVE2 Buddy. Docs: /usr/share/doc/buddy/AGENTS.md (orientation), api.md (HTTP API)." \
    "Also served at http://<this-board>/llms.txt"
