#!/usr/bin/env python
#
# Copyright (C) 2014 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Given a target-files zipfile that does not contain images (ie, does
not have an IMAGES/ top-level subdirectory), produce the images and
add them to the zipfile.

Usage:  add_img_to_target_files [flag] target_files

  -a  (--add_missing)
      Build and add missing images to "IMAGES/". If this option is
      not specified, this script will simply exit when "IMAGES/"
      directory exists in the target file.

  -r  (--rebuild_recovery)
      Rebuild the recovery patch and write it to the system image. Only
      meaningful when system image needs to be rebuilt and there're separate
      boot / recovery images.

  --replace_verity_private_key
      Replace the private key used for verity signing. (same as the option
      in sign_target_files_apks)

  --replace_verity_public_key
       Replace the certificate (public key) used for verity verification. (same
       as the option in sign_target_files_apks)

  --is_signing
      Skip building & adding the images for "userdata" and "cache" if we
      are signing the target files.
"""

from __future__ import print_function

import datetime
import logging
import os
import shlex
import shutil
import stat
import sys
import uuid
import tempfile
import zipfile

import build_image
import build_super_image
import common
import verity_utils
import ota_metadata_pb2

from apex_utils import GetApexInfoFromTargetFiles
from common import AddCareMapForAbOta, ZipDelete

if sys.hexversion < 0x02070000:
  print("Python 2.7 or newer is required.", file=sys.stderr)
  sys.exit(1)

logger = logging.getLogger(__name__)

OPTIONS = common.OPTIONS
OPTIONS.add_missing = False
OPTIONS.rebuild_recovery = False
OPTIONS.replace_updated_files_list = []
OPTIONS.replace_verity_public_key = False
OPTIONS.replace_verity_private_key = False
OPTIONS.is_signing = False

# Use a fixed timestamp (01/01/2009 00:00:00 UTC) for files when packaging
# images. (b/24377993, b/80600931)
FIXED_FILE_TIMESTAMP = int((
    datetime.datetime(2009, 1, 1, 0, 0, 0, 0, None) -
    datetime.datetime.utcfromtimestamp(0)).total_seconds())


class OutputFile(object):
  """A helper class to write a generated file to the given dir or zip.

  When generating images, we want the outputs to go into the given zip file, or
  the given dir.

  Attributes:
    name: The name of the output file, regardless of the final destination.
  """

  def __init__(self, output_zip, input_dir, *args):
    # We write the intermediate output file under the given input_dir, even if
    # the final destination is a zip archive.
    self.name = os.path.join(input_dir, *args)
    self._output_zip = output_zip
    if self._output_zip:
      self._zip_name = os.path.join(*args)

  def Write(self, compress_type=None):
    if self._output_zip:
      common.ZipWrite(self._output_zip, self.name,
                      self._zip_name, compress_type=compress_type)


def AddSystem(output_zip, recovery_img=None, boot_img=None):
  """Turn the contents of SYSTEM into a system image and store it in
  output_zip. Returns the name of the system image file."""

  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "system.img")
  if os.path.exists(img.name):
    logger.info("system.img already exists; no need to rebuild...")
    return img.name

  def output_sink(fn, data):
    output_file = os.path.join(OPTIONS.input_tmp, "SYSTEM", fn)
    with open(output_file, "wb") as ofile:
      ofile.write(data)

    if output_zip:
      arc_name = "SYSTEM/" + fn
      if arc_name in output_zip.namelist():
        OPTIONS.replace_updated_files_list.append(arc_name)
      else:
        common.ZipWrite(output_zip, output_file, arc_name)

  board_uses_vendorimage = OPTIONS.info_dict.get(
      "board_uses_vendorimage") == "true"

  if (OPTIONS.rebuild_recovery and not board_uses_vendorimage and
          recovery_img is not None and boot_img is not None):
    logger.info("Building new recovery patch on system at system/vendor")
    common.MakeRecoveryPatch(OPTIONS.input_tmp, output_sink, recovery_img,
                             boot_img, info_dict=OPTIONS.info_dict)

  block_list = OutputFile(output_zip, OPTIONS.input_tmp,
                          "IMAGES", "system.map")
  CreateImage(OPTIONS.input_tmp, OPTIONS.info_dict, "system", img,
              block_list=block_list)
  return img.name


def AddSystemOther(output_zip):
  """Turn the contents of SYSTEM_OTHER into a system_other image
  and store it in output_zip."""

  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "system_other.img")
  if os.path.exists(img.name):
    logger.info("system_other.img already exists; no need to rebuild...")
    return

  CreateImage(OPTIONS.input_tmp, OPTIONS.info_dict, "system_other", img)


def AddVendor(output_zip, recovery_img=None, boot_img=None):
  """Turn the contents of VENDOR into a vendor image and store in it
  output_zip."""

  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "vendor.img")
  if os.path.exists(img.name):
    logger.info("vendor.img already exists; no need to rebuild...")
    return img.name

  def output_sink(fn, data):
    output_file = os.path.join(OPTIONS.input_tmp, "VENDOR", fn)
    with open(output_file, "wb") as ofile:
      ofile.write(data)

    if output_zip:
      arc_name = "VENDOR/" + fn
      if arc_name in output_zip.namelist():
        OPTIONS.replace_updated_files_list.append(arc_name)
      else:
        common.ZipWrite(output_zip, output_file, arc_name)

  board_uses_vendorimage = OPTIONS.info_dict.get(
      "board_uses_vendorimage") == "true"

  if (OPTIONS.rebuild_recovery and board_uses_vendorimage and
          recovery_img is not None and boot_img is not None):
    logger.info("Building new recovery patch on vendor")
    common.MakeRecoveryPatch(OPTIONS.input_tmp, output_sink, recovery_img,
                             boot_img, info_dict=OPTIONS.info_dict)

  block_list = OutputFile(output_zip, OPTIONS.input_tmp,
                          "IMAGES", "vendor.map")
  CreateImage(OPTIONS.input_tmp, OPTIONS.info_dict, "vendor", img,
              block_list=block_list)
  return img.name


def AddProduct(output_zip):
  """Turn the contents of PRODUCT into a product image and store it in
  output_zip."""

  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "product.img")
  if os.path.exists(img.name):
    logger.info("product.img already exists; no need to rebuild...")
    return img.name

  block_list = OutputFile(
      output_zip, OPTIONS.input_tmp, "IMAGES", "product.map")
  CreateImage(
      OPTIONS.input_tmp, OPTIONS.info_dict, "product", img,
      block_list=block_list)
  return img.name


def AddSystemExt(output_zip):
  """Turn the contents of SYSTEM_EXT into a system_ext image and store it in
  output_zip."""

  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES",
                   "system_ext.img")
  if os.path.exists(img.name):
    logger.info("system_ext.img already exists; no need to rebuild...")
    return img.name

  block_list = OutputFile(
      output_zip, OPTIONS.input_tmp, "IMAGES", "system_ext.map")
  CreateImage(
      OPTIONS.input_tmp, OPTIONS.info_dict, "system_ext", img,
      block_list=block_list)
  return img.name


def AddOdm(output_zip):
  """Turn the contents of ODM into an odm image and store it in output_zip."""

  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "odm.img")
  if os.path.exists(img.name):
    logger.info("odm.img already exists; no need to rebuild...")
    return img.name

  block_list = OutputFile(
      output_zip, OPTIONS.input_tmp, "IMAGES", "odm.map")
  CreateImage(
      OPTIONS.input_tmp, OPTIONS.info_dict, "odm", img,
      block_list=block_list)
  return img.name


def AddVendorDlkm(output_zip):
  """Turn the contents of VENDOR_DLKM into an vendor_dlkm image and store it in output_zip."""

  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "vendor_dlkm.img")
  if os.path.exists(img.name):
    logger.info("vendor_dlkm.img already exists; no need to rebuild...")
    return img.name

  block_list = OutputFile(
      output_zip, OPTIONS.input_tmp, "IMAGES", "vendor_dlkm.map")
  CreateImage(
      OPTIONS.input_tmp, OPTIONS.info_dict, "vendor_dlkm", img,
      block_list=block_list)
  return img.name


def AddOdmDlkm(output_zip):
  """Turn the contents of OdmDlkm into an odm_dlkm image and store it in output_zip."""

  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "odm_dlkm.img")
  if os.path.exists(img.name):
    logger.info("odm_dlkm.img already exists; no need to rebuild...")
    return img.name

  block_list = OutputFile(
      output_zip, OPTIONS.input_tmp, "IMAGES", "odm_dlkm.map")
  CreateImage(
      OPTIONS.input_tmp, OPTIONS.info_dict, "odm_dlkm", img,
      block_list=block_list)
  return img.name

def AddSystemDlkm(output_zip):
  """Turn the contents of SystemDlkm into an system_dlkm image and store it in output_zip."""

  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "system_dlkm.img")
  if os.path.exists(img.name):
    logger.info("system_dlkm.img already exists; no need to rebuild...")
    return img.name

  block_list = OutputFile(
      output_zip, OPTIONS.input_tmp, "IMAGES", "system_dlkm.map")
  CreateImage(
      OPTIONS.input_tmp, OPTIONS.info_dict, "system_dlkm", img,
      block_list=block_list)
  return img.name


def AddDtbo(output_zip):
  """Adds the DTBO image.

  Uses the image under IMAGES/ if it already exists. Otherwise looks for the
  image under PREBUILT_IMAGES/, signs it as needed, and returns the image name.
  """
  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "dtbo.img")
  if os.path.exists(img.name):
    logger.info("dtbo.img already exists; no need to rebuild...")
    return img.name

  dtbo_prebuilt_path = os.path.join(
      OPTIONS.input_tmp, "PREBUILT_IMAGES", "dtbo.img")
  assert os.path.exists(dtbo_prebuilt_path)
  shutil.copy(dtbo_prebuilt_path, img.name)

  # AVB-sign the image as needed.
  if OPTIONS.info_dict.get("avb_enable") == "true":
    # Signing requires +w
    os.chmod(img.name, os.stat(img.name).st_mode | stat.S_IWUSR)

    avbtool = OPTIONS.info_dict["avb_avbtool"]
    part_size = OPTIONS.info_dict["dtbo_size"]
    # The AVB hash footer will be replaced if already present.
    cmd = [avbtool, "add_hash_footer", "--image", img.name,
           "--partition_size", str(part_size), "--partition_name", "dtbo"]
    common.AppendAVBSigningArgs(cmd, "dtbo")
    args = OPTIONS.info_dict.get("avb_dtbo_add_hash_footer_args")
    if args and args.strip():
      cmd.extend(shlex.split(args))
    common.RunAndCheckOutput(cmd)

  img.Write()
  return img.name


def AddPvmfw(output_zip):
  """Adds the pvmfw image.

  Uses the image under IMAGES/ if it already exists. Otherwise looks for the
  image under PREBUILT_IMAGES/, signs it as needed, and returns the image name.
  """
  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "pvmfw.img")
  if os.path.exists(img.name):
    logger.info("pvmfw.img already exists; no need to rebuild...")
    return img.name

  pvmfw_prebuilt_path = os.path.join(
      OPTIONS.input_tmp, "PREBUILT_IMAGES", "pvmfw.img")
  assert os.path.exists(pvmfw_prebuilt_path)
  shutil.copy(pvmfw_prebuilt_path, img.name)

  # AVB-sign the image as needed.
  if OPTIONS.info_dict.get("avb_enable") == "true":
    # Signing requires +w
    os.chmod(img.name, os.stat(img.name).st_mode | stat.S_IWUSR)

    avbtool = OPTIONS.info_dict["avb_avbtool"]
    part_size = OPTIONS.info_dict["pvmfw_size"]
    # The AVB hash footer will be replaced if already present.
    cmd = [avbtool, "add_hash_footer", "--image", img.name,
           "--partition_size", str(part_size), "--partition_name", "pvmfw"]
    common.AppendAVBSigningArgs(cmd, "pvmfw")
    args = OPTIONS.info_dict.get("avb_pvmfw_add_hash_footer_args")
    if args and args.strip():
      cmd.extend(shlex.split(args))
    common.RunAndCheckOutput(cmd)

  img.Write()
  return img.name


def AddCustomImages(output_zip, partition_name):
  """Adds and signs custom images in IMAGES/.

  Args:
    output_zip: The output zip file (needs to be already open), or None to
        write images to OPTIONS.input_tmp/.

  Uses the image under IMAGES/ if it already exists. Otherwise looks for the
  image under PREBUILT_IMAGES/, signs it as needed, and returns the image name.

  Raises:
    AssertionError: If image can't be found.
  """

  key_path = OPTIONS.info_dict.get("avb_{}_key_path".format(partition_name))
  algorithm = OPTIONS.info_dict.get("avb_{}_algorithm".format(partition_name))
  extra_args = OPTIONS.info_dict.get(
      "avb_{}_add_hashtree_footer_args".format(partition_name))
  partition_size = OPTIONS.info_dict.get(
      "avb_{}_partition_size".format(partition_name))

  builder = verity_utils.CreateCustomImageBuilder(
      OPTIONS.info_dict, partition_name, partition_size,
      key_path, algorithm, extra_args)

  for img_name in OPTIONS.info_dict.get(
          "avb_{}_image_list".format(partition_name)).split():
    custom_image = OutputFile(
        output_zip, OPTIONS.input_tmp, "IMAGES", img_name)
    if os.path.exists(custom_image.name):
      continue

    custom_image_prebuilt_path = os.path.join(
        OPTIONS.input_tmp, "PREBUILT_IMAGES", img_name)
    assert os.path.exists(custom_image_prebuilt_path), \
        "Failed to find %s at %s" % (img_name, custom_image_prebuilt_path)

    shutil.copy(custom_image_prebuilt_path, custom_image.name)

    if builder is not None:
      builder.Build(custom_image.name)

    custom_image.Write()

  default = os.path.join(OPTIONS.input_tmp, "IMAGES", partition_name + ".img")
  assert os.path.exists(default), \
      "There should be one %s.img" % (partition_name)
  return default


def CreateImage(input_dir, info_dict, what, output_file, block_list=None):
  logger.info("creating %s.img...", what)

  image_props = build_image.ImagePropFromGlobalDict(info_dict, what)
  image_props["timestamp"] = FIXED_FILE_TIMESTAMP

  if what == "system":
    fs_config_prefix = ""
  else:
    fs_config_prefix = what + "_"

  fs_config = os.path.join(
      input_dir, "META/" + fs_config_prefix + "filesystem_config.txt")
  if not os.path.exists(fs_config):
    fs_config = None

  # Override values loaded from info_dict.
  if fs_config:
    image_props["fs_config"] = fs_config
  if block_list:
    image_props["block_list"] = block_list.name

  # Use repeatable ext4 FS UUID and hash_seed UUID (based on partition name and
  # build fingerprint). Also use the legacy build id, because the vbmeta digest
  # isn't available at this point.
  build_info = common.BuildInfo(info_dict, use_legacy_id=True)
  uuid_seed = what + "-" + build_info.GetPartitionFingerprint(what)
  image_props["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, uuid_seed))
  hash_seed = "hash_seed-" + uuid_seed
  image_props["hash_seed"] = str(uuid.uuid5(uuid.NAMESPACE_URL, hash_seed))

  build_image.BuildImage(
      os.path.join(input_dir, what.upper()), image_props, output_file.name)

  output_file.Write()
  if block_list:
    block_list.Write()

  # Set the '_image_size' for given image size.
  is_verity_partition = "verity_block_device" in image_props
  verity_supported = (image_props.get("verity") == "true" or
                      image_props.get("avb_enable") == "true")
  is_avb_enable = image_props.get("avb_hashtree_enable") == "true"
  if verity_supported and (is_verity_partition or is_avb_enable):
    image_size = image_props.get("image_size")
    if image_size:
      image_size_key = what + "_image_size"
      info_dict[image_size_key] = int(image_size)

  use_dynamic_size = (
      info_dict.get("use_dynamic_partition_size") == "true" and
      what in shlex.split(info_dict.get("dynamic_partition_list", "").strip()))
  if use_dynamic_size:
    info_dict.update(build_image.GlobalDictFromImageProp(image_props, what))


def AddUserdata(output_zip):
  """Create a userdata image and store it in output_zip.

  In most case we just create and store an empty userdata.img;
  But the invoker can also request to create userdata.img with real
  data from the target files, by setting "userdata_img_with_data=true"
  in OPTIONS.info_dict.
  """

  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "userdata.img")
  if os.path.exists(img.name):
    logger.info("userdata.img already exists; no need to rebuild...")
    return

  # Skip userdata.img if no size.
  image_props = build_image.ImagePropFromGlobalDict(OPTIONS.info_dict, "data")
  if not image_props.get("partition_size"):
    return

  logger.info("creating userdata.img...")

  image_props["timestamp"] = FIXED_FILE_TIMESTAMP

  if OPTIONS.info_dict.get("userdata_img_with_data") == "true":
    user_dir = os.path.join(OPTIONS.input_tmp, "DATA")
  else:
    user_dir = common.MakeTempDir()

  build_image.BuildImage(user_dir, image_props, img.name)

  common.CheckSize(img.name, "userdata.img", OPTIONS.info_dict)
  # Always use compression for useradata image.
  # As it's likely huge and consist of lots of 0s.
  img.Write(zipfile.ZIP_DEFLATED)


def AddVBMeta(output_zip, partitions, name, needed_partitions):
  """Creates a VBMeta image and stores it in output_zip.

  It generates the requested VBMeta image. The requested image could be for
  top-level or chained VBMeta image, which is determined based on the name.

  Args:
    output_zip: The output zip file, which needs to be already open.
    partitions: A dict that's keyed by partition names with image paths as
        values. Only valid partition names are accepted, as partitions listed
        in common.AVB_PARTITIONS and custom partitions listed in
        OPTIONS.info_dict.get("avb_custom_images_partition_list")
    name: Name of the VBMeta partition, e.g. 'vbmeta', 'vbmeta_system'.
    needed_partitions: Partitions whose descriptors should be included into the
        generated VBMeta image.

  Returns:
    Path to the created image.

  Raises:
    AssertionError: On invalid input args.
  """
  assert needed_partitions, "Needed partitions must be specified"

  img = OutputFile(
      output_zip, OPTIONS.input_tmp, "IMAGES", "{}.img".format(name))
  if os.path.exists(img.name):
    logger.info("%s.img already exists; not rebuilding...", name)
    return img.name

  common.BuildVBMeta(img.name, partitions, name, needed_partitions)
  img.Write()
  return img.name


def AddPartitionTable(output_zip):
  """Create a partition table image and store it in output_zip."""

  img = OutputFile(
      output_zip, OPTIONS.input_tmp, "IMAGES", "partition-table.img")
  bpt = OutputFile(
      output_zip, OPTIONS.input_tmp, "META", "partition-table.bpt")

  # use BPTTOOL from environ, or "bpttool" if empty or not set.
  bpttool = os.getenv("BPTTOOL") or "bpttool"
  cmd = [bpttool, "make_table", "--output_json", bpt.name,
         "--output_gpt", img.name]
  input_files_str = OPTIONS.info_dict["board_bpt_input_files"]
  input_files = input_files_str.split(" ")
  for i in input_files:
    cmd.extend(["--input", i])
  disk_size = OPTIONS.info_dict.get("board_bpt_disk_size")
  if disk_size:
    cmd.extend(["--disk_size", disk_size])
  args = OPTIONS.info_dict.get("board_bpt_make_table_args")
  if args:
    cmd.extend(shlex.split(args))
  common.RunAndCheckOutput(cmd)

  img.Write()
  bpt.Write()


def AddCache(output_zip):
  """Create an empty cache image and store it in output_zip."""

  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "cache.img")
  if os.path.exists(img.name):
    logger.info("cache.img already exists; no need to rebuild...")
    return

  image_props = build_image.ImagePropFromGlobalDict(OPTIONS.info_dict, "cache")
  # The build system has to explicitly request for cache.img.
  if "fs_type" not in image_props:
    return

  logger.info("creating cache.img...")

  image_props["timestamp"] = FIXED_FILE_TIMESTAMP

  user_dir = common.MakeTempDir()
  build_image.BuildImage(user_dir, image_props, img.name)

  common.CheckSize(img.name, "cache.img", OPTIONS.info_dict)
  img.Write()


def CheckAbOtaImages(output_zip, ab_partitions):
  """Checks that all the listed A/B partitions have their images available.

  The images need to be available under IMAGES/ or RADIO/, with the former takes
  a priority.

  Args:
    output_zip: The output zip file (needs to be already open), or None to
        find images in OPTIONS.input_tmp/.
    ab_partitions: The list of A/B partitions.

  Raises:
    AssertionError: If it can't find an image.
  """
  for partition in ab_partitions:
    img_name = partition + ".img"
    bin_name = partition + ".bin"

    # Assert that the image is present under IMAGES/ now.
    if output_zip:
      # Zip spec says: All slashes MUST be forward slashes.
      images_path = "IMAGES/" + img_name
      radio_path = "RADIO/" + img_name
      bin_images_path = "IMAGES/" + bin_name
      bin_radio_path = "RADIO/" + bin_name
      available = (images_path in output_zip.namelist() or
                   radio_path in output_zip.namelist() or 
                   bin_images_path in output_zip.namelist() or
                   bin_radio_path in output_zip.namelist())

    else:
      images_path = os.path.join(OPTIONS.input_tmp, "IMAGES", img_name)
      radio_path = os.path.join(OPTIONS.input_tmp, "RADIO", img_name)
      bin_images_path = os.path.join(OPTIONS.input_tmp, "IMAGES", bin_name)
      bin_radio_path = os.path.join(OPTIONS.input_tmp, "RADIO", bin_name)
      available = (os.path.exists(images_path) or
                   os.path.exists(radio_path) or 
                   os.path.exists(bin_images_path) or 
                   os.path.exists(bin_radio_path))

    assert available, "Failed to find " + img_name


def AddPackRadioImages(output_zip, images):
  """Copies images listed in META/pack_radioimages.txt from RADIO/ to IMAGES/.

  Args:
    output_zip: The output zip file (needs to be already open), or None to
        write images to OPTIONS.input_tmp/.
    images: A list of image names.

  Raises:
    AssertionError: If a listed image can't be found.
  """
  for image in images:
    img_name = image.strip()
    _, ext = os.path.splitext(img_name)
    if not ext:
      img_name += ".img"

    prebuilt_path = os.path.join(OPTIONS.input_tmp, "IMAGES", img_name)
    if os.path.exists(prebuilt_path):
      logger.info("%s already exists, no need to overwrite...", img_name)
      continue

    img_radio_path = os.path.join(OPTIONS.input_tmp, "RADIO", img_name)
    assert os.path.exists(img_radio_path), \
        "Failed to find %s at %s" % (img_name, img_radio_path)

    if output_zip:
      common.ZipWrite(output_zip, img_radio_path, "IMAGES/" + img_name)
    else:
      shutil.copy(img_radio_path, prebuilt_path)


def AddSuperEmpty(output_zip):
  """Create a super_empty.img and store it in output_zip."""

  img = OutputFile(output_zip, OPTIONS.input_tmp, "IMAGES", "super_empty.img")
  build_super_image.BuildSuperImage(OPTIONS.info_dict, img.name)
  img.Write()


def AddSuperSplit(output_zip):
  """Create split super_*.img and store it in output_zip."""

  outdir = os.path.join(OPTIONS.input_tmp, "OTA")
  built = build_super_image.BuildSuperImage(OPTIONS.input_tmp, outdir)

  if built:
    for dev in OPTIONS.info_dict['super_block_devices'].strip().split():
      img = OutputFile(output_zip, OPTIONS.input_tmp, "OTA",
                       "super_" + dev + ".img")
      img.Write()


def ReplaceUpdatedFiles(zip_filename, files_list):
  """Updates all the ZIP entries listed in files_list.

  For now the list includes META/care_map.pb, and the related files under
  SYSTEM/ after rebuilding recovery.
  """
  common.ZipDelete(zip_filename, files_list)
  output_zip = zipfile.ZipFile(zip_filename, "a",
                               compression=zipfile.ZIP_DEFLATED,
                               allowZip64=True)
  for item in files_list:
    file_path = os.path.join(OPTIONS.input_tmp, item)
    assert os.path.exists(file_path)
    common.ZipWrite(output_zip, file_path, arcname=item)
  common.ZipClose(output_zip)


def HasPartition(partition_name):
  """Determines if the target files archive should build a given partition."""

  return ((os.path.isdir(
      os.path.join(OPTIONS.input_tmp, partition_name.upper())) and
      OPTIONS.info_dict.get(
      "building_{}_image".format(partition_name)) == "true") or
      os.path.exists(
      os.path.join(OPTIONS.input_tmp, "IMAGES",
                   "{}.img".format(partition_name))))


def AddApexInfo(output_zip):
  apex_infos = GetApexInfoFromTargetFiles(OPTIONS.input_tmp, 'system',
                                          compressed_only=False)
  apex_metadata_proto = ota_metadata_pb2.ApexMetadata()
  apex_metadata_proto.apex_info.extend(apex_infos)
  apex_info_bytes = apex_metadata_proto.SerializeToString()

  output_file = os.path.join(OPTIONS.input_tmp, "META", "apex_info.pb")
  with open(output_file, "wb") as ofile:
    ofile.write(apex_info_bytes)
  if output_zip:
    arc_name = "META/apex_info.pb"
    if arc_name in output_zip.namelist():
      OPTIONS.replace_updated_files_list.append(arc_name)
    else:
      common.ZipWrite(output_zip, output_file, arc_name)


def AddVbmetaDigest(output_zip):
  """Write the vbmeta digest to the output dir and zipfile."""

  # Calculate the vbmeta digest and put the result in to META/
  boot_images = OPTIONS.info_dict.get("boot_images")
  # Disable the digest calculation if the target_file is used as a container
  # for boot images. A boot container might contain boot-5.4.img, boot-5.10.img
  # etc., instead of just a boot.img and will fail in vbmeta digest calculation.
  boot_container = boot_images and (
      len(boot_images.split()) >= 2 or boot_images.split()[0] != 'boot.img')
  if (OPTIONS.info_dict.get("avb_enable") == "true" and not boot_container and
          OPTIONS.info_dict.get("avb_building_vbmeta_image") == "true"):
    avbtool = OPTIONS.info_dict["avb_avbtool"]
    digest = verity_utils.CalculateVbmetaDigest(OPTIONS.input_tmp, avbtool)
    vbmeta_digest_txt = os.path.join(OPTIONS.input_tmp, "META",
                                     "vbmeta_digest.txt")
    with open(vbmeta_digest_txt, 'w') as f:
      f.write(digest)
    # writes to the output zipfile
    if output_zip:
      arc_name = "META/vbmeta_digest.txt"
      if arc_name in output_zip.namelist():
        OPTIONS.replace_updated_files_list.append(arc_name)
      else:
        common.ZipWriteStr(output_zip, arc_name, digest)


def AddImagesToTargetFiles(filename):
  """Creates and adds images (boot/recovery/system/...) to a target_files.zip.

  It works with either a zip file (zip mode), or a directory that contains the
  files to be packed into a target_files.zip (dir mode). The latter is used when
  being called from build/make/core/Makefile.

  The images will be created under IMAGES/ in the input target_files.zip.

  Args:
    filename: the target_files.zip, or the zip root directory.
  """
  if os.path.isdir(filename):
    OPTIONS.input_tmp = os.path.abspath(filename)
  else:
    OPTIONS.input_tmp = common.UnzipTemp(filename)

  if not OPTIONS.add_missing:
    if os.path.isdir(os.path.join(OPTIONS.input_tmp, "IMAGES")):
      logger.warning("target_files appears to already contain images.")
      sys.exit(1)

  OPTIONS.info_dict = common.LoadInfoDict(OPTIONS.input_tmp, repacking=True)

  has_recovery = OPTIONS.info_dict.get("no_recovery") != "true"
  has_boot = OPTIONS.info_dict.get("no_boot") != "true"
  has_init_boot = OPTIONS.info_dict.get("init_boot") == "true"
  has_vendor_boot = OPTIONS.info_dict.get("vendor_boot") == "true"
  has_vendor_kernel_boot = OPTIONS.info_dict.get("vendor_kernel_boot") == "true"

  # {vendor,odm,product,system_ext,vendor_dlkm,odm_dlkm, system_dlkm, system, system_other}.img
  # can be built from source, or  dropped into target_files.zip as a prebuilt blob.
  has_vendor = HasPartition("vendor")
  has_odm = HasPartition("odm")
  has_vendor_dlkm = HasPartition("vendor_dlkm")
  has_odm_dlkm = HasPartition("odm_dlkm")
  has_system_dlkm = HasPartition("system_dlkm")
  has_product = HasPartition("product")
  has_system_ext = HasPartition("system_ext")
  has_system = HasPartition("system")
  has_system_other = HasPartition("system_other")
  has_userdata = OPTIONS.info_dict.get("building_userdata_image") == "true"
  has_cache = OPTIONS.info_dict.get("building_cache_image") == "true"

  # Set up the output destination. It writes to the given directory for dir
  # mode; otherwise appends to the given ZIP.
  if os.path.isdir(filename):
    output_zip = None
  else:
    output_zip = zipfile.ZipFile(filename, "a",
                                 compression=zipfile.ZIP_DEFLATED,
                                 allowZip64=True)

  # Always make input_tmp/IMAGES available, since we may stage boot / recovery
  # images there even under zip mode. The directory will be cleaned up as part
  # of OPTIONS.input_tmp.
  images_dir = os.path.join(OPTIONS.input_tmp, "IMAGES")
  if not os.path.isdir(images_dir):
    os.makedirs(images_dir)

  # A map between partition names and their paths, which could be used when
  # generating AVB vbmeta image.
  partitions = {}

  def banner(s):
    logger.info("\n\n++++ %s  ++++\n\n", s)

  boot_image = None
  if has_boot:
    banner("boot")
    boot_images = OPTIONS.info_dict.get("boot_images")
    if boot_images is None:
      boot_images = "boot.img"
    for index, b in enumerate(boot_images.split()):
      # common.GetBootableImage() returns the image directly if present.
      boot_image = common.GetBootableImage(
          "IMAGES/" + b, b, OPTIONS.input_tmp, "BOOT")
      # boot.img may be unavailable in some targets (e.g. aosp_arm64).
      if boot_image:
        boot_image_path = os.path.join(OPTIONS.input_tmp, "IMAGES", b)
        # Although multiple boot images can be generated, include the image
        # descriptor of only the first boot image in vbmeta
        if index == 0:
          partitions['boot'] = boot_image_path
        if not os.path.exists(boot_image_path):
          boot_image.WriteToDir(OPTIONS.input_tmp)
          if output_zip:
            boot_image.AddToZip(output_zip)

  if has_init_boot:
    banner("init_boot")
    init_boot_image = common.GetBootableImage(
        "IMAGES/init_boot.img", "init_boot.img", OPTIONS.input_tmp, "INIT_BOOT")
    if init_boot_image:
      partitions['init_boot'] = os.path.join(
          OPTIONS.input_tmp, "IMAGES", "init_boot.img")
      if not os.path.exists(partitions['init_boot']):
        init_boot_image.WriteToDir(OPTIONS.input_tmp)
        if output_zip:
          init_boot_image.AddToZip(output_zip)

  if has_vendor_boot:
    banner("vendor_boot")
    vendor_boot_image = common.GetVendorBootImage(
        "IMAGES/vendor_boot.img", "vendor_boot.img", OPTIONS.input_tmp,
        "VENDOR_BOOT")
    if vendor_boot_image:
      partitions['vendor_boot'] = os.path.join(OPTIONS.input_tmp, "IMAGES",
                                               "vendor_boot.img")
      if not os.path.exists(partitions['vendor_boot']):
        vendor_boot_image.WriteToDir(OPTIONS.input_tmp)
        if output_zip:
          vendor_boot_image.AddToZip(output_zip)

  if has_vendor_kernel_boot:
    banner("vendor_kernel_boot")
    vendor_kernel_boot_image = common.GetVendorKernelBootImage(
        "IMAGES/vendor_kernel_boot.img", "vendor_kernel_boot.img", OPTIONS.input_tmp,
        "VENDOR_KERNEL_BOOT")
    if vendor_kernel_boot_image:
      partitions['vendor_kernel_boot'] = os.path.join(OPTIONS.input_tmp, "IMAGES",
                                               "vendor_kernel_boot.img")
      if not os.path.exists(partitions['vendor_kernel_boot']):
        vendor_kernel_boot_image.WriteToDir(OPTIONS.input_tmp)
        if output_zip:
          vendor_kernel_boot_image.AddToZip(output_zip)

  recovery_image = None
  if has_recovery:
    banner("recovery")
    recovery_image = common.GetBootableImage(
        "IMAGES/recovery.img", "recovery.img", OPTIONS.input_tmp, "RECOVERY")
    assert recovery_image, "Failed to create recovery.img."
    partitions['recovery'] = os.path.join(
        OPTIONS.input_tmp, "IMAGES", "recovery.img")
    if not os.path.exists(partitions['recovery']):
      recovery_image.WriteToDir(OPTIONS.input_tmp)
      if output_zip:
        recovery_image.AddToZip(output_zip)

      banner("recovery (two-step image)")
      # The special recovery.img for two-step package use.
      recovery_two_step_image = common.GetBootableImage(
          "OTA/recovery-two-step.img", "recovery-two-step.img",
          OPTIONS.input_tmp, "RECOVERY", two_step_image=True)
      assert recovery_two_step_image, "Failed to create recovery-two-step.img."
      recovery_two_step_image_path = os.path.join(
          OPTIONS.input_tmp, "OTA", "recovery-two-step.img")
      if not os.path.exists(recovery_two_step_image_path):
        recovery_two_step_image.WriteToDir(OPTIONS.input_tmp)
        if output_zip:
          recovery_two_step_image.AddToZip(output_zip)

  def add_partition(partition, has_partition, add_func, add_args):
    if has_partition:
      banner(partition)
      partitions[partition] = add_func(output_zip, *add_args)

  add_partition_calls = (
      ("system", has_system, AddSystem, [recovery_image, boot_image]),
      ("vendor", has_vendor, AddVendor, [recovery_image, boot_image]),
      ("product", has_product, AddProduct, []),
      ("system_ext", has_system_ext, AddSystemExt, []),
      ("odm", has_odm, AddOdm, []),
      ("vendor_dlkm", has_vendor_dlkm, AddVendorDlkm, []),
      ("odm_dlkm", has_odm_dlkm, AddOdmDlkm, []),
      ("system_dlkm", has_system_dlkm, AddSystemDlkm, []),
      ("system_other", has_system_other, AddSystemOther, []),
  )
  for call in add_partition_calls:
    add_partition(*call)

  AddApexInfo(output_zip)

  if not OPTIONS.is_signing:
    banner("userdata")
    AddUserdata(output_zip)
    banner("cache")
    AddCache(output_zip)

  if OPTIONS.info_dict.get("board_bpt_enable") == "true":
    banner("partition-table")
    AddPartitionTable(output_zip)

  add_partition("dtbo",
                OPTIONS.info_dict.get("has_dtbo") == "true", AddDtbo, [])
  add_partition("pvmfw",
                OPTIONS.info_dict.get("has_pvmfw") == "true", AddPvmfw, [])

  # Custom images.
  custom_partitions = OPTIONS.info_dict.get(
      "avb_custom_images_partition_list", "").strip().split()
  for partition_name in custom_partitions:
    partition_name = partition_name.strip()
    banner("custom images for " + partition_name)
    partitions[partition_name] = AddCustomImages(output_zip, partition_name)

  if OPTIONS.info_dict.get("avb_enable") == "true":
    # vbmeta_partitions includes the partitions that should be included into
    # top-level vbmeta.img, which are the ones that are not included in any
    # chained VBMeta image plus the chained VBMeta images themselves.
    # Currently custom_partitions are all chained to VBMeta image.
    vbmeta_partitions = common.AVB_PARTITIONS[:] + tuple(custom_partitions)

    vbmeta_system = OPTIONS.info_dict.get("avb_vbmeta_system", "").strip()
    if vbmeta_system:
      banner("vbmeta_system")
      partitions["vbmeta_system"] = AddVBMeta(
          output_zip, partitions, "vbmeta_system", vbmeta_system.split())
      vbmeta_partitions = [
          item for item in vbmeta_partitions
          if item not in vbmeta_system.split()]
      vbmeta_partitions.append("vbmeta_system")

    vbmeta_vendor = OPTIONS.info_dict.get("avb_vbmeta_vendor", "").strip()
    if vbmeta_vendor:
      banner("vbmeta_vendor")
      partitions["vbmeta_vendor"] = AddVBMeta(
          output_zip, partitions, "vbmeta_vendor", vbmeta_vendor.split())
      vbmeta_partitions = [
          item for item in vbmeta_partitions
          if item not in vbmeta_vendor.split()]
      vbmeta_partitions.append("vbmeta_vendor")

    if OPTIONS.info_dict.get("avb_building_vbmeta_image") == "true":
      banner("vbmeta")
      AddVBMeta(output_zip, partitions, "vbmeta", vbmeta_partitions)

  if OPTIONS.info_dict.get("use_dynamic_partitions") == "true":
    if OPTIONS.info_dict.get("build_super_empty_partition") == "true":
      banner("super_empty")
      AddSuperEmpty(output_zip)

  if OPTIONS.info_dict.get("build_super_partition") == "true":
    if OPTIONS.info_dict.get(
            "build_retrofit_dynamic_partitions_ota_package") == "true":
      banner("super split images")
      AddSuperSplit(output_zip)

  banner("radio")
  ab_partitions_txt = os.path.join(OPTIONS.input_tmp, "META",
                                   "ab_partitions.txt")
  if os.path.exists(ab_partitions_txt):
    with open(ab_partitions_txt) as f:
      ab_partitions = f.read().splitlines()

    # For devices using A/B update, make sure we have all the needed images
    # ready under IMAGES/ or RADIO/.
    CheckAbOtaImages(output_zip, ab_partitions)

    # Generate care_map.pb for ab_partitions, then write this file to
    # target_files package.
    output_care_map = os.path.join(OPTIONS.input_tmp, "META", "care_map.pb")
    AddCareMapForAbOta(output_zip if output_zip else output_care_map,
                       ab_partitions, partitions)

  # Radio images that need to be packed into IMAGES/, and product-img.zip.
  pack_radioimages_txt = os.path.join(
      OPTIONS.input_tmp, "META", "pack_radioimages.txt")
  if os.path.exists(pack_radioimages_txt):
    with open(pack_radioimages_txt) as f:
      AddPackRadioImages(output_zip, f.readlines())

  AddVbmetaDigest(output_zip)

  if output_zip:
    common.ZipClose(output_zip)
    if OPTIONS.replace_updated_files_list:
      ReplaceUpdatedFiles(output_zip.filename,
                          OPTIONS.replace_updated_files_list)


def OptimizeCompressedEntries(zipfile_path):
  """Convert files that do not compress well to uncompressed storage

  EROFS images tend to be compressed already, so compressing them again
  yields little space savings. Leaving them uncompressed will make
  downstream tooling's job easier, and save compute time.
  """
  if not zipfile.is_zipfile(zipfile_path):
    return
  entries_to_store = []
  with tempfile.TemporaryDirectory() as tmpdir:
    with zipfile.ZipFile(zipfile_path, "r", allowZip64=True) as zfp:
      for zinfo in zfp.filelist:
        if not zinfo.filename.startswith("IMAGES/") and not zinfo.filename.startswith("META"):
          continue
        # Don't try to store userdata.img uncompressed, it's usually huge.
        if zinfo.filename.endswith("userdata.img"):
          continue
        if zinfo.compress_size > zinfo.file_size * 0.80 and zinfo.compress_type != zipfile.ZIP_STORED:
          entries_to_store.append(zinfo)
          zfp.extract(zinfo, tmpdir)
    if len(entries_to_store) == 0:
      return
    # Remove these entries, then re-add them as ZIP_STORED
    ZipDelete(zipfile_path, [entry.filename for entry in entries_to_store])
    with zipfile.ZipFile(zipfile_path, "a", allowZip64=True) as zfp:
      for entry in entries_to_store:
        zfp.write(os.path.join(tmpdir, entry.filename), entry.filename, compress_type=zipfile.ZIP_STORED)


def main(argv):
  def option_handler(o, a):
    if o in ("-a", "--add_missing"):
      OPTIONS.add_missing = True
    elif o in ("-r", "--rebuild_recovery",):
      OPTIONS.rebuild_recovery = True
    elif o == "--replace_verity_private_key":
      OPTIONS.replace_verity_private_key = (True, a)
    elif o == "--replace_verity_public_key":
      OPTIONS.replace_verity_public_key = (True, a)
    elif o == "--is_signing":
      OPTIONS.is_signing = True
    else:
      return False
    return True

  args = common.ParseOptions(
      argv, __doc__, extra_opts="ar",
      extra_long_opts=["add_missing", "rebuild_recovery",
                       "replace_verity_public_key=",
                       "replace_verity_private_key=",
                       "is_signing"],
      extra_option_handler=option_handler)

  if len(args) != 1:
    common.Usage(__doc__)
    sys.exit(1)

  common.InitLogging()

  AddImagesToTargetFiles(args[0])
  OptimizeCompressedEntries(args[0])
  logger.info("done.")


if __name__ == '__main__':
  try:
    common.CloseInheritedPipes()
    main(sys.argv[1:])
  finally:
    common.Cleanup()
