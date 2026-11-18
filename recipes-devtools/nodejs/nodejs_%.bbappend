# Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
# SPDX-License-Identifier: MIT
# Contributors: KC
# Maintainer: KC

# Was nodejs_22.11.0.bbappend, which appended to a version no layer provided
# and so did nothing for months. Wildcarded so it cannot silently stop
# applying again on the next version bump.

inherit clang-native

TC_CXX_RUNTIME:class-native = "llvm"

# Node 22 calls ares_dns_write() and ares_query_dnsrec(), which arrived in
# c-ares 1.28; scarthgap ships older. Use Node's bundled copy instead of the
# system one. nodejs is a build-host tool here and is in no image, so this
# does not affect the deliverable or its SBOM.
PACKAGECONFIG:remove = "ares"
