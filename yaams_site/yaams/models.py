from django.db import models
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
import memcache
import email
import yaams_exceptions
import logging
import textwrap
from utils import nl2br, escape, check_password, make_hash, parse_hash
import types
import json

class NewsPost(models.Model):
    name = models.CharField(max_length = 256)
    created = models.DateTimeField(auto_now_add = True)
    content = models.TextField()
    
    @classmethod
    def get_posts(cls):
        """
        Returns the posts as a list of NewsPost instances with the latest being first
        """
        return NewsPost.objects.order_by('-created')[:10]
        
    @classmethod
    def create_post(cls, title, content):
        p = NewsPost(name = title, content = content)
        p.save()
        
    def __str__(self):
        return self.name

class EmailUser(models.Model):
    """
    Represents a unique email user in a database. Allows us to check for various attributes of the email addresses inbox.
    """
    email_address = models.CharField(max_length = 257)
    password_protected = models.BooleanField(default = False)
    password_hash = models.CharField(max_length=52)
            
    def __str__(self):
        return self.email_address
    @staticmethod
    def get_obj(email_addr):
        try:
            return EmailUser.objects.get(email_address = email_addr)
        except ObjectDoesNotExist:
            u = EmailUser(email_address = email_addr, password_protected = False, password_hash = '')
            u.save()
            u.user_mailbox = None
            return u
            
    def check_password(self, password):
        """
        Checks the supplied password against the stored password in the database
        returns True if they match, False otherwise
        """
        return check_password(password, self.password_hash, settings.INBOX_SETTINGS['hash_secret'])
    
    def fetch_inbox(self):
        return Mailbox(self.email_address)
            
    def get_inbox(self, password = ''):
        if self.has_password():
            if self.check_password(password):
                return self.fetch_inbox()
            else:
                raise yaams_exceptions.PasswordProtected(self.email_address)
        else:
            return self.fetch_inbox()

        
    def has_password(self):
        return self.password_protected
    
    def set_password(self, password):
        """
        Sets a password on a the inbox that this instance represents.
        password -- plaintext string representation of the password
        """
        if self.has_password():
            #We already have a password and a salt ^_^
            hash, salt = parse_hash(self.password_hash)
            new_hash = make_hash(settings.INBOX_SETTINGS['hash_secret'], password, salt)
            self.password_hash = new_hash
            self.save()
        else:
            #We never had a password or don't have one set atm
            new_hash = make_hash(settings.INBOX_SETTINGS['hash_secret'], password)
            #i = EmailUser(email_address = self.email_address, password_protected = True, password_hash = new_hash)
            #i.save()
            self.password_protected = True
            self.password_hash = new_hash
            self.save()
            

class Mailbox:
	"""Represents a single users mailbox in a memcached instance. MemCacheInstance is a Static or Class variable that is accessible by all instances"""
	#Static Variables
	MemCacheInstance = memcache.Client(settings.MEMCACHE['hosts'], settings.MEMCACHE['debug']);
	
	#Instance Variables
	email_address = ''
	mail_list = []
	
	def __init__(self, address):
		"""Initializes the object to fetch the users email from memcache"""
		self.email_address = address.encode("ASCII")
		self.mail_list = Mailbox.MemCacheInstance.get(self.email_address)
		if (self.mail_list == None):
			raise yaams_exceptions.NoSuchUser(address)
	
	def get_message_count(self):
		"""Returns the number of messages in the users mailbox"""
		return len(self.mail_list)
	
	def get_latest_messages(self, count, json_encode = False):
		"""
		Returns a list of up to count items with the latest items in the mailbox at the beginning of the list
		NOTE: The messages returned are instances of the Message Class
		"""
		output_list = []
		
		tlist = self.mail_list[:]
		tlist.reverse()
		
		for i in range(int(count)):
                    try:
                        if (json_encode):
                            output_list.append(Message(tlist[i]).get_as_json())
                        else:
                            output_list.append(Message(tlist[i]))
                    except IndexError:
                        break
                if (json_encode):
                    output_dict = {'msg_count': len(output_list), 'messages' : output_list }
                    return json.dumps(output_dict)
                else:
                    return output_list
                
        def get_messages(self):
            """
            Returns all of the message in the inbox as a list of dictionaries
            """
            return self.mail_list
            
        
        def get_message(self, msg_id, json_encode = False):
            """
            Returns the message with an id of msg_id
            """
            t = self.get_messages()
            out_msg = 0
            
            for msg in t:
                if msg['Message_ID'] == int(msg_id):
                    out_msg = Message(msg)
                    break;
            
            
            if out_msg == 0:
                raise yaams_exceptions.NoSuchMessage(msg_id)
            
            try:
                if (json_encode):
                    return out_msg.get_as_json();
                else:
                    return out_msg
            except IndexError:
                raise yaams_exceptions.NoSuchMessage(msg_id)
	
	def mark_message_read(self, index):
		"""
		Marks message at the specified index in the mail_list as read in memcache
		There is a risk of this function causing a message that is recieved at the exact same time that this function stores its
		updated mailbox list to be lost. This is the cost of using memcached as it isn't meant for this purpose. I could design my way 
		around it by storing each message separately and only updating that message but it would cause slowdown. This application isn't meant to be a 100% mailbox
		and as such this mark_message_read() function won't be used at the moment but a placeholder is here for future expansion.
		"""
		pass
        
        def delete_message(self, msg_id):
            """
            Deletes a message from the users inbox.
            
            Returns True on success or False on error
            """
            msg_id = int(msg_id)
            for i in range(0, len(self.mail_list)):
                tMsg = Message(self.mail_list[i])
                
                #logger = logging.getLogger('django.request')
                #logger.debug('tMsg ID is ' + str(tMsg.get_id()))
                #logger.debug('Search ID is ' + str(msg_id))
                
                if tMsg.get_id() == msg_id:
                    del self.mail_list[i]
                    Mailbox.MemCacheInstance.set(self.email_address, self.mail_list)
                    return True
            return False

class Message:
	"""Encapsulages the email packet and adds a little bit of customization such as get_readstate()"""
	#Instance Variables
	is_read = False;
	msg_text = ''
	parsed_obj = ''
        msg_id = 0
	
	def __init__(self, message_dict):
		"""Takes the plaintext format of a email message and creates an object out of it that is easily accessed"""
		self.is_read = message_dict['Read_State']
		self.msg_text = message_dict['Message']
		self.parsed_obj = email.message_from_string(self.msg_text)
                self.msg_id = message_dict['Message_ID']
	
	def get_content(self):
		"""Returns the content of the message"""
                
                text = self.parsed_obj.get_payload()
                
                output = ''
                
                if isinstance(text, types.ListType) and self.parsed_obj.is_multipart():
                    for i in text:
                        if i.get_filename():
                            logging.error('Attachment found!!!')
                            output += "\nSorry but the attachment has been stripped from this message!"
                            continue
                        if (i.get_content_type() == "text/plain" or i.get_content_type() == "text/html"):
                            output+=(i.get_payload()) + '\n\n'
                            #logging.debug("Appending %s to output" % i.get_payload())
                else:
                    output = text
                    
                #logging.error("get_content(): Output before wrap is %s" % output)
                
                output = escape(output)
                output = nl2br(output)
                output1 = textwrap.wrap(output, 65)
                output = ''
                for i in output1:
                    output += i + '<br />'
                    
                #logging.error("get_content returning %s" % output)
                
		return output
	
        def get_id(self):
            return self.msg_id
        
	def get_sender(self):
		"""Returns the sender's address as a string"""
		return self.parsed_obj.get("From")
	
	def get_received_date(self):
		"""Returns the date that the message was recieved"""
		return self.parsed_obj.get("Date")
	
	def get_read_state(self):
		"""Returns whether this message is marked as read or not"""
		return self.is_read
	
	def get_subject(self):
		"""Returns the subject of the message"""
		return self.parsed_obj.get("Subject")
            
        def get_as_json(self):
            """Returns a Message Object as an JSON encoded dictionary"""
            output_dict = {}
            output_dict['subject'] = self.get_subject()
            output_dict['recieved_date'] = self.get_received_date()
            output_dict['sender'] = self.get_sender()
            output_dict['content'] = self.get_content()
            output_dict['msg_id'] = self.get_id()
            output_dict['read_state'] = self.get_read_state()
            
            return json.dumps(output_dict);
        
