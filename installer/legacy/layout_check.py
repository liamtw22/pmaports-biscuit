"""Read-only offline biscuit v2 layout classifier. No device access or repair API.

This is a host-side candidate, not a TWRP executable or an apply authorization.
Only regular image files are opened. Bounded readers also accept saved metadata
captures; missing bytes always reject rather than being synthesized as zeros.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import uuid
import zlib

SECTOR = 512
ENTRIES = 128
ENTRY_SIZE = 128
LINUX_DATA = uuid.UUID('0fc63daf-8483-4772-8e79-3d69d8477de4').bytes_le
PREFIX = (
 ('kb',2048,4095), ('dkb',4096,6143), ('lk_a',32768,34815),
 ('tee1',49152,59391), ('lk_b',65536,67583), ('tee2',81920,92159),
 ('expdb',98304,118783), ('misc',118784,119808), ('persist',131072,163839),
 ('boot_a',163840,196607), ('boot_b',196608,229375), ('recovery',229376,262143),
)
STOCK_DATA = (('system_a',294912,1867775),('system_b',1867776,3440639),
              ('cache',3440640,5046271))

class Rejected(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Rejected(reason)


class FileReader:
    """O_RDONLY regular images only; rejects block devices, links and changing files."""
    def __init__(self, path):
        path = Path(path)
        require(not path.is_symlink(), 'input_symlink')
        before = path.stat()
        require(stat.S_ISREG(before.st_mode), 'regular_image_required')
        self._file = path.open('rb', buffering=0)
        opened = os.fstat(self._file.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            self._file.close()
            raise Rejected('input_identity_changed')
        self._identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        self.size = opened.st_size

    def read(self, offset, length):
        require(type(offset) is int and type(length) is int and 0 <= offset <= self.size
                and 0 <= length <= 65536 and length <= self.size-offset, 'read_bounds')
        self._file.seek(offset)
        value = self._file.read(length)
        require(len(value) == length, 'short_read')
        return value

    def unchanged(self):
        s = os.fstat(self._file.fileno())
        require((s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns) == self._identity, 'image_changed')

    def close(self):
        self._file.close()


class CaptureReader:
    """Bounded sparse metadata captures, never implicit zero-fill for missing data."""
    def __init__(self, size, regions):
        require(type(size) is int and size > 0, 'invalid_capacity')
        self.size = size
        self.regions = tuple(sorted((offset, bytes(data)) for offset,data in regions))
        previous = 0
        for offset,data in self.regions:
            require(type(offset) is int and previous <= offset and offset+len(data) <= size,
                    'capture_overlap_or_bounds')
            previous = offset+len(data)

    def read(self, offset, length):
        require(type(offset) is int and type(length) is int and offset >= 0
                and 0 <= length <= 65536 and offset+length <= self.size, 'read_bounds')
        for start,data in self.regions:
            if start <= offset and offset+length <= start+len(data):
                return data[offset-start:offset-start+length]
        raise Rejected('missing_capture_range')


class SliceReader:
    def __init__(self, reader, offset, size):
        require(0 <= offset and 0 < size and offset+size <= reader.size, 'slice_bounds')
        self.reader,self.offset,self.size = reader,offset,size

    def read(self, offset, length):
        require(0 <= offset and 0 <= length and offset+length <= self.size, 'slice_read_bounds')
        return self.reader.read(self.offset+offset,length)


def _header(raw):
    require(len(raw) == SECTOR and raw[:8] == b'EFI PART', 'gpt_header_signature')
    revision,size,crc,reserved = struct.unpack_from('<4I',raw,8)
    require(revision == 0x10000 and size == 92 and reserved == 0, 'gpt_header_format')
    checked = bytearray(raw[:size]); checked[16:20] = b'\0'*4
    require(zlib.crc32(checked)&0xffffffff == crc, 'gpt_header_crc')
    require(not any(raw[size:]), 'gpt_header_trailing_data')
    current,backup,first,last = struct.unpack_from('<4Q',raw,24)
    array,count,entry_size,array_crc = struct.unpack_from('<Q3I',raw,72)
    require(count == ENTRIES and entry_size == ENTRY_SIZE, 'gpt_array_shape')
    require(any(raw[56:72]), 'zero_disk_guid')
    return dict(current=current,backup=backup,first=first,last=last,array=array,
                count=count,entry_size=entry_size,array_crc=array_crc,disk_guid=raw[56:72])


def validate_gpt(reader, *, allow_amonet_pmbr=False):
    require(type(reader.size) is int and reader.size % SECTOR == 0
            and reader.size >= SECTOR*68, 'capacity_not_512_sector_aligned')
    total = reader.size//SECTOR
    mbr = reader.read(0,SECTOR)
    require(mbr[510:512] == b'\x55\xaa', 'protective_mbr_signature')
    records = [mbr[446+i*16:462+i*16] for i in range(4)]
    standard_pmbr = (records[0][0] == 0 and records[0][4] == 0xee
                     and struct.unpack_from('<II',records[0],8) == (1,min(total-1,0xffffffff)))
    # Supplied amonet v2 and saved reference GPT use this exact saturated MBR
    # record even below 2 TiB. Recognize outer metadata only; do not normalize
    # or derive capacity from it. The two GPTs still bind actual reader capacity.
    vendor_pmbr = (allow_amonet_pmbr and records[0] ==
                   bytes.fromhex('00000200eeffffff01000000ffffffff'))
    require((standard_pmbr or vendor_pmbr) and not any(b''.join(records[1:])),
            'protective_mbr_extent_or_hybrid')
    p = _header(reader.read(SECTOR,SECTOR))
    b = _header(reader.read((total-1)*SECTOR,SECTOR))
    require((p['current'],p['backup'],p['array']) == (1,total-1,2), 'primary_geometry')
    require((b['current'],b['backup'],b['array']) == (total-1,1,total-33), 'backup_geometry')
    require((p['first'],p['last']) == (34,total-34), 'usable_bounds')
    for key in ('first','last','count','entry_size','array_crc','disk_guid'):
        require(p[key] == b[key], 'gpt_pair_disagreement')
    arr = reader.read(2*SECTOR,ENTRIES*ENTRY_SIZE)
    arr2 = reader.read((total-33)*SECTOR,ENTRIES*ENTRY_SIZE)
    require(arr == arr2, 'gpt_arrays_disagree')
    require(zlib.crc32(arr)&0xffffffff == p['array_crc'], 'gpt_array_crc')
    entries=[]; empty=False
    for i in range(ENTRIES):
        raw = arr[i*ENTRY_SIZE:(i+1)*ENTRY_SIZE]
        if not any(raw[:16]):
            require(not any(raw), 'nonzero_unused_entry')
            empty=True
            continue
        require(not empty, 'gpt_entry_hole')
        require(any(raw[16:32]), 'zero_partition_guid')
        first,last,attrs = struct.unpack_from('<3Q',raw,32)
        require(p['first'] <= first <= last <= p['last'], 'partition_bounds')
        try:
            name = raw[56:].decode('utf-16le')
        except UnicodeDecodeError as exc:
            raise Rejected('partition_name_encoding') from exc
        name = name.rstrip('\0')
        require(name and '\0' not in name and all(32 <= ord(c) < 127 for c in name), 'partition_name')
        entries.append(dict(number=i+1,name=name,first=first,last=last,attributes=attrs,
                            type=raw[:16],guid=raw[16:32]))
    require(entries, 'empty_partition_table')
    require(len({r['name'] for r in entries}) == len(entries), 'duplicate_name')
    require(len({r['guid'] for r in entries}) == len(entries), 'duplicate_partition_guid')
    ordered=sorted(entries,key=lambda r:r['first'])
    require(all(a['last'] < b['first'] for a,b in zip(ordered,ordered[1:])), 'partition_overlap')
    return dict(total=total,last=p['last'],entries=entries,
                pmbr='standard' if standard_pmbr else 'amonet_saturated_32bit',
                table_sha256=hashlib.sha256(arr).hexdigest(),
                metadata_sha256=hashlib.sha256(mbr+reader.read(SECTOR,SECTOR)+arr+arr2+
                    reader.read((total-1)*SECTOR,SECTOR)).hexdigest())


def outer_kind(gpt):
    rows=gpt['entries']
    # Names alone do not establish the boot chain. Exact geometry/type/attributes
    # are required even for the read-only stock-geometry classification.
    triples=tuple((r['name'],r['first'],r['last']) for r in rows)
    types_ok=all(r['type']==LINUX_DATA and r['attributes']==0 for r in rows)
    if types_ok and triples == PREFIX+STOCK_DATA+(('userdata',5046272,gpt['last']),):
        return 'v2_stock_geometry'
    last=gpt['last']
    # The bootable merged layout: amonet v2's bootloader looks system_a up by
    # name and hangs without it, so two 1 MiB stubs end the disk.
    if types_ok and triples == PREFIX+(('userdata',294912,last-4096),
                                       ('system_a',last-4095,last-2048),
                                       ('system_b',last-2047,last)):
        return 'v2_merged_geometry'
    # The first merged layout, without the stubs. Recognised so it can be
    # repaired, never accepted: it cannot boot.
    if types_ok and triples == PREFIX+(('userdata',294912,last),):
        return 'v2_merged_without_stubs'
    if any(r['name'].endswith('_x') for r in rows):
        return 'legacy_or_intermediate_refused'
    return 'unknown_geometry'


def ext4_probe(reader):
    """Structural probe only; no group/inode/journal integrity or stock provenance claim."""
    sb=reader.read(1024,1024)
    require(struct.unpack_from('<H',sb,56)[0] == 0xef53, 'no_ext_superblock')
    blocks=struct.unpack_from('<I',sb,4)[0]
    log=struct.unpack_from('<I',sb,24)[0]
    require(log in (0,1,2), 'unqualified_ext_block_size')
    block_size=1024<<log
    inodes=struct.unpack_from('<I',sb,0)[0]
    first=struct.unpack_from('<I',sb,20)[0]
    bpg=struct.unpack_from('<I',sb,32)[0]
    ipg=struct.unpack_from('<I',sb,40)[0]
    state=struct.unpack_from('<H',sb,58)[0]
    rev=struct.unpack_from('<I',sb,76)[0]
    inode_size=struct.unpack_from('<H',sb,88)[0]
    compat,incompat,ro=struct.unpack_from('<3I',sb,92)
    require(rev == 1 and inode_size in (128,256) and inodes > 0 and blocks > 0, 'ext_geometry')
    require(first == (1 if block_size==1024 else 0)
            and 0 < bpg <= block_size*8 and 0 < ipg <= block_size*8
            and inodes <= ((blocks-first+bpg-1)//bpg)*ipg, 'ext_group_geometry')
    require(blocks*block_size <= reader.size and blocks > first, 'ext_exceeds_container')
    require(any(sb[104:120]), 'zero_ext_uuid')
    require(state == 1 and not (incompat & 4), 'ext_unclean_or_recovery_needed')
    # Deliberately narrow probe subset, NOT an e2fsprogs 1.43.3 feature-support map.
    # 64bit, metadata_csum, encryption, bigalloc, orphan_file etc require separately
    # qualified feature-aware code/checkers. Do not infer them from version text.
    require(compat & ~0x3c == 0 and incompat & ~0x242 == 0 and ro & ~0x7b == 0,
            'ext_features_unqualified')
    require(compat & 4 and incompat & 0x40, 'journaled_extents_required')
    return dict(kind='clean_ext4_structural_candidate',block_size=block_size,blocks=blocks,
                features={'compat':compat,'incompat':incompat,'ro_compat':ro},
                integrity_verified=False,stock_filesystem_identity_verified=False)


def classify(reader):
    result=dict(schema='biscuit.recovery.readonly.v1',repair_allowed=False,
                mount_allowed=False,format_allowed=False,conversion_allowed=False,
                amonet_version_verified=False,layout='unknown',userdata='unknown')
    try:
        gpt=validate_gpt(reader,allow_amonet_pmbr=True); kind=outer_kind(gpt)
        result.update(layout=kind,total_sectors=gpt['total'],last_usable_lba=gpt['last'],
                      outer_metadata_sha256=gpt['metadata_sha256'],pmbr=gpt['pmbr'])
        require(kind in ('v2_stock_geometry','v2_merged_geometry'), 'source_layout_refused')
        row=[r for r in gpt['entries'] if r['name']=='userdata'][0]
        data=SliceReader(reader,row['first']*SECTOR,(row['last']-row['first']+1)*SECTOR)
        # Both GPT signatures and a partition-bearing MBR veto the raw-ext path.
        head=data.read(0,SECTOR*4)
        tail=data.read(data.size-SECTOR,SECTOR)
        partition_evidence=(head[SECTOR:SECTOR+8]==b'EFI PART' or tail[:8]==b'EFI PART'
                            or (head[510:512]==b'\x55\xaa' and any(head[446:510])))
        if partition_evidence:
            result['userdata']='partitioned_or_damaged_container'
            nested=validate_gpt(data)
            result['userdata']='validated_nested_gpt'
            result['nested_metadata_sha256']=nested['metadata_sha256']
            result['pmos_image_identity_verified']=False
            result['reason']='raw_userdata_repair_forbidden'
        elif kind == 'v2_merged_geometry':
            result['reason']='merged_userdata_never_stock_repair'
        else:
            result['ext4']=ext4_probe(data)
            result['userdata']='stock_geometry_ext4_candidate'
            result['reason']='full_stock_identity_and_filesystem_check_still_required'
        if hasattr(reader,'unchanged'):
            reader.unchanged()
    except (Rejected,OSError) as exc:
        result['reason']=str(exc) if isinstance(exc,Rejected) else 'input_io_error'
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image',help='Offline regular image only; no block devices')
    args=parser.parse_args()
    reader=None
    try:
        reader=FileReader(args.image)
        result=classify(reader)
    except (Rejected,OSError):
        result={'schema':'biscuit.recovery.readonly.v1','repair_allowed':False,
                'conversion_allowed':False,'reason':'input_refused'}
    finally:
        if reader is not None: reader.close()
    print(json.dumps(result,sort_keys=True))
    # No successful exit can be mistaken for permission to modify a device.
    return 2

if __name__=='__main__':
    raise SystemExit(main())
